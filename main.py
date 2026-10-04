from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
import base64, hashlib, json, os, secrets, time
from urllib.parse import urlencode
import httpx
from cryptography.fernet import Fernet, InvalidToken

API='https://api.derivws.com'
AUTH='https://auth.deriv.com/oauth2'
app=FastAPI(title='Deriv Signal Lab',version='0.5.0')
app.add_middleware(CORSMiddleware,allow_origins=['*'],allow_methods=['*'],allow_headers=['*'])

def cfg(name, default=''):
    return os.getenv(name,default).strip()

def fernet():
    secret=cfg('SESSION_SECRET')
    if not secret: raise HTTPException(503,'SESSION_SECRET is not configured')
    key=base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())
    return Fernet(key)

def app_id():
    return cfg('DERIV_APP_ID') or cfg('DERIV_CLIENT_ID')

def auth_headers(session:dict):
    # OAuth tokens identify the app on their own; PATs must carry Deriv-App-ID.
    h={'Authorization':f"Bearer {session['t']}"}
    if session.get('k')=='pat' and session.get('a'): h['Deriv-App-ID']=session['a']
    return h

def session_from(request:Request)->dict:
    raw=request.cookies.get('deriv_session')
    if not raw: raise HTTPException(401,'Not connected to Deriv')
    try: data=fernet().decrypt(raw.encode()).decode()
    except InvalidToken: raise HTTPException(401,'Deriv session expired or invalid')
    try: obj=json.loads(data)
    except ValueError: obj={'t':data,'k':'oauth'}
    if not obj.get('t'): raise HTTPException(401,'Not connected to Deriv')
    return obj

def set_session(response:Response,session:dict,max_age:int):
    enc=fernet().encrypt(json.dumps(session).encode()).decode()
    response.set_cookie('deriv_session',enc,httponly=True,secure=cfg('DERIV_REDIRECT_URI').startswith('https://'),samesite='lax',max_age=max_age)

@app.get('/',response_class=HTMLResponse)
async def home():
    return HTMLResponse(open(__file__.replace('main.py','index.html'),encoding='utf-8').read())

@app.get('/api/config')
async def config():
    return {'oauth_ready':bool(cfg('DERIV_CLIENT_ID') and cfg('DERIV_REDIRECT_URI') and cfg('SESSION_SECRET')),
            'pat_ready':bool(cfg('SESSION_SECRET')),'app_id':app_id(),
            'client_id':cfg('DERIV_CLIENT_ID'),'redirect_uri':cfg('DERIV_REDIRECT_URI'),
            'trade_budget':{'per_minute':360,'per_hour':14400,'app_soft_per_minute':180}}

@app.get('/api/auth/start')
async def auth_start(request:Request,response:Response,challenge:str):
    client=cfg('DERIV_CLIENT_ID'); redirect=cfg('DERIV_REDIRECT_URI')
    if not client or not redirect: raise HTTPException(503,'DERIV_CLIENT_ID / DERIV_REDIRECT_URI not configured')
    if len(challenge)<40: raise HTTPException(400,'Invalid PKCE challenge')
    state=secrets.token_urlsafe(24)
    response.set_cookie('oauth_state',state,httponly=True,secure=redirect.startswith('https://'),samesite='lax',max_age=600)
    params={'response_type':'code','client_id':client,'redirect_uri':redirect,'scope':'trade','state':state,'code_challenge':challenge,'code_challenge_method':'S256'}
    return {'url':AUTH+'/auth?'+urlencode(params)}

class Exchange(BaseModel):
    code:str; state:str; verifier:str

@app.post('/api/auth/exchange')
async def auth_exchange(body:Exchange,request:Request,response:Response):
    expected=request.cookies.get('oauth_state')
    if not expected or not secrets.compare_digest(expected,body.state): raise HTTPException(400,'OAuth state mismatch')
    async with httpx.AsyncClient(timeout=15) as c:
        r=await c.post(AUTH+'/token',data={'grant_type':'authorization_code','client_id':cfg('DERIV_CLIENT_ID'),'code':body.code,'code_verifier':body.verifier,'redirect_uri':cfg('DERIV_REDIRECT_URI')},headers={'Content-Type':'application/x-www-form-urlencoded'})
    if r.status_code>=400: raise HTTPException(r.status_code,'Deriv token exchange failed')
    tok=r.json().get('access_token')
    if not tok: raise HTTPException(502,'No access token returned')
    set_session(response,{'t':tok,'k':'oauth'},min(int(r.json().get('expires_in',3600)),3600))
    response.delete_cookie('oauth_state')
    return {'connected':True,'method':'oauth','expires_in':r.json().get('expires_in',3600)}

class PatLogin(BaseModel):
    token:str; app_id:str=''

@app.post('/api/auth/pat')
async def auth_pat(body:PatLogin,response:Response):
    tok=body.token.strip(); aid=(body.app_id or app_id()).strip()
    if not tok: raise HTTPException(400,'API token is required')
    if not aid: raise HTTPException(400,'Deriv App ID is required for token login (set DERIV_APP_ID or enter one)')
    session={'t':tok,'k':'pat','a':aid}
    async with httpx.AsyncClient(timeout=15) as c: r=await c.get(API+'/trading/v1/options/accounts',headers=auth_headers(session))
    if r.status_code>=400: raise HTTPException(r.status_code,'Deriv rejected the token: '+r.text[:200])
    set_session(response,session,12*3600)
    return {'connected':True,'method':'pat','expires_in':12*3600}

@app.post('/api/auth/logout')
async def logout(response:Response):
    response.delete_cookie('deriv_session'); return {'connected':False}

@app.get('/api/accounts')
async def accounts(request:Request):
    sess=session_from(request)
    async with httpx.AsyncClient(timeout=15) as c: r=await c.get(API+'/trading/v1/options/accounts',headers=auth_headers(sess))
    if r.status_code>=400: raise HTTPException(r.status_code,r.text[:300])
    return r.json()

@app.post('/api/ws-url/{account_id}')
async def ws_url(account_id:str,request:Request):
    sess=session_from(request)
    async with httpx.AsyncClient(timeout=15) as c: r=await c.post(f'{API}/trading/v1/options/accounts/{account_id}/otp',headers=auth_headers(sess))
    if r.status_code>=400: raise HTTPException(r.status_code,r.text[:300])
    data=r.json().get('data',{})
    if not data.get('url'): raise HTTPException(502,'Deriv did not return a WebSocket URL')
    return {'url':data['url']}

@app.get('/api/public-ws')
async def public_ws(): return {'url':'wss://api.derivws.com/trading/v1/options/ws/public'}
