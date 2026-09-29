from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
import base64, hashlib, os, secrets
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

def token_from(request:Request):
    raw=request.cookies.get('deriv_session')
    if not raw: raise HTTPException(401,'Not connected to Deriv')
    try: return fernet().decrypt(raw.encode()).decode()
    except InvalidToken: raise HTTPException(401,'Deriv session expired or invalid')

@app.get('/',response_class=HTMLResponse)
async def home():
    return HTMLResponse(open(__file__.replace('main.py','index.html'),encoding='utf-8').read())

@app.get('/api/config')
async def config():
    return {'oauth_ready':bool(cfg('DERIV_CLIENT_ID') and cfg('DERIV_REDIRECT_URI') and cfg('SESSION_SECRET')),
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
    enc=fernet().encrypt(tok.encode()).decode()
    response.set_cookie('deriv_session',enc,httponly=True,secure=cfg('DERIV_REDIRECT_URI').startswith('https://'),samesite='lax',max_age=min(int(r.json().get('expires_in',3600)),3600))
    response.delete_cookie('oauth_state')
    return {'connected':True,'expires_in':r.json().get('expires_in',3600)}

@app.post('/api/auth/logout')
async def logout(response:Response):
    response.delete_cookie('deriv_session'); return {'connected':False}

@app.get('/api/accounts')
async def accounts(request:Request):
    tok=token_from(request)
    async with httpx.AsyncClient(timeout=15) as c: r=await c.get(API+'/trading/v1/options/accounts',headers={'Authorization':f'Bearer {tok}'})
    if r.status_code>=400: raise HTTPException(r.status_code,r.text[:300])
    return r.json()

@app.post('/api/ws-url/{account_id}')
async def ws_url(account_id:str,request:Request):
    tok=token_from(request)
    async with httpx.AsyncClient(timeout=15) as c: r=await c.post(f'{API}/trading/v1/options/accounts/{account_id}/otp',headers={'Authorization':f'Bearer {tok}'})
    if r.status_code>=400: raise HTTPException(r.status_code,r.text[:300])
    data=r.json().get('data',{})
    if not data.get('url'): raise HTTPException(502,'Deriv did not return a WebSocket URL')
    return {'url':data['url']}

@app.get('/api/public-ws')
async def public_ws(): return {'url':'wss://api.derivws.com/trading/v1/options/ws/public'}
