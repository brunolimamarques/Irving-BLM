import os
import json
from flask import Flask, render_template, request, jsonify, redirect
import firebase_admin
from firebase_admin import credentials, firestore, auth
import requests
import pandas as pd
from datetime import datetime, timedelta
import urllib.parse

app = Flask(__name__)

# --- 1. INICIALIZAÇÃO FIREBASE ---
firebase_cred_string = os.environ.get("FIREBASE_JSON")
try:
    if firebase_cred_string:
        cred_dict = json.loads(firebase_cred_string)
        cred = credentials.Certificate(cred_dict)
    else:
        cred = credentials.Certificate("firebase-chave.json")
        
    if not firebase_admin._apps:
        firebase_admin.initialize_app(cred)
    db = firestore.client()
except Exception as e:
    print(f"Erro Firebase: {e}")

# --- 2. CREDENCIAIS ML ---
ML_APP_ID = "1096855357952882"
ML_SECRET_KEY = "vzOhLT31AxYEqS4JJ9qfuoYGZtsbg1AM"
REDIRECT_URI = "https://irving-blm.vercel.app/callback"

def verificar_token(req):
    auth_header = req.headers.get('Authorization')
    if not auth_header or not auth_header.startswith('Bearer '): return None
    try:
        return auth.verify_id_token(auth_header.split(' ')[1])['uid']
    except:
        return None

# --- 3. LÓGICA DE AUTO-REFRESH ---
def gerenciar_token_ml(uid, user_doc):
    ml_token = user_doc.get('ml_access_token')
    refresh_token = user_doc.get('ml_refresh_token')
    
    # Tenta uma chamada simples para ver se o token está ativo
    teste_url = "https://api.mercadolibre.com/users/me"
    res = requests.get(teste_url, headers={"Authorization": f"Bearer {ml_token}"})
    
    if res.status_code == 401: # Token Expirado!
        print(f"Token expirado para {uid}. Renovando...")
        url_refresh = "https://api.mercadolibre.com/oauth/token"
        payload = {
            "grant_type": "refresh_token",
            "client_id": ML_APP_ID,
            "client_secret": ML_SECRET_KEY,
            "refresh_token": refresh_token
        }
        refresh_res = requests.post(url_refresh, data=payload).json()
        
        if "access_token" in refresh_res:
            ml_token = refresh_res['access_token']
            db.collection('usuarios').document(uid).update({
                'ml_access_token': ml_token,
                'ml_refresh_token': refresh_res.get('refresh_token', refresh_token)
            })
            return ml_token
    return ml_token

# --- 4. ROTAS ---
@app.route('/')
def home():
    return render_template('index.html')

@app.route('/conectar-ml')
def conectar_ml():
    uid = request.args.get('uid')
    if not uid: return "Usuário não identificado", 400
    parametros = {"response_type": "code", "client_id": ML_APP_ID, "redirect_uri": REDIRECT_URI, "state": uid, "prompt": "consent"}
    return redirect("https://auth.mercadolivre.com.br/authorization?" + urllib.parse.urlencode(parametros))

@app.route('/callback')
def callback():
    code = request.args.get('code')
    uid = request.args.get('state')
    url_token = "https://api.mercadolibre.com/oauth/token"
    payload = {"grant_type": "authorization_code", "client_id": ML_APP_ID, "client_secret": ML_SECRET_KEY, "code": code, "redirect_uri": REDIRECT_URI}
    resposta = requests.post(url_token, data=payload).json()
    if "access_token" in resposta:
        db.collection('usuarios').document(uid).set({
            'ml_access_token': resposta['access_token'],
            'ml_refresh_token': resposta.get('refresh_token'),
            'ml_user_id': resposta.get('user_id'),
            'status_ml': 'conectado'
        }, merge=True)
        return redirect('/?status=sucesso')
    return "Erro ao conectar."

@app.route('/api/salvar_custo', methods=['POST'])
def salvar_custo():
    uid = verificar_token(request)
    if not uid: return jsonify({"erro": "Acesso Negado"}), 401
    dados = request.json
    db.collection('custos').document(uid).set({dados.get('item_id'): float(dados.get('custo', 0))}, merge=True)
    return jsonify({"status": "sucesso"})

@app.route('/api/salvar_imposto', methods=['POST'])
def salvar_imposto():
    uid = verificar_token(request)
    if not uid: return jsonify({"erro": "Acesso Negado"}), 401
    imposto = float(request.json.get('imposto', 0))
    db.collection('configuracoes').document(uid).set({'imposto_padrao': imposto}, merge=True)
    return jsonify({"status": "sucesso"})

@app.route('/api/dados')
def api_dados():
    uid = verificar_token(request)
    if not uid: return jsonify({"erro": "Acesso Negado."}), 401

    periodo_dias = int(request.args.get('periodo', 30))
    user_doc = db.collection('usuarios').document(uid).get().to_dict()
    if not user_doc or 'ml_access_token' not in user_doc: return jsonify({"erro": "ml_nao_conectado"}), 403

    # --- AUTO-REFRESH EM AÇÃO ---
    ml_token = gerenciar_token_ml(uid, user_doc)
    ml_seller_id = user_doc['ml_user_id']
    
    custos_db = db.collection('custos').document(uid).get().to_dict() or {}
    config_db = db.collection('configuracoes').document(uid).get().to_dict() or {}
    imposto_padrao_pct = float(config_db.get('imposto_padrao', 0))
    
    headers = {"Authorization": f"Bearer {ml_token}"}
    data_inicio = (datetime.utcnow() - timedelta(days=periodo_dias)).strftime('%Y-%m-%dT00:00:00.000-00:00')
    
    # URL de Busca Ampla (Recent) para garantir que traga dados
    url_vendas = f"https://api.mercadolibre.com/orders/search?seller={ml_seller_id}&order.date_created.from={data_inicio}"
    
    try:
        resposta_ml = requests.get(url_vendas, headers=headers).json()
        resultados = resposta_ml.get('results', [])
        
        if not resultados: return jsonify({"erro": "vazio", "imposto_padrao": imposto_padrao_pct})

        agrupado = {}
        for order in resultados:
            destino = order.get('shipping', {}).get('receiver_address', {}).get('country', {}).get('id', 'BR')
            custo_envio_unidade = 0 if destino == 'AR' else 18.50 
            
            for item in order.get('order_items', []):
                item_id = item['item']['id']
                title = item['item']['title']
                qty = item['quantity']
                price = item['unit_price']
                
                custo_cmv = float(custos_db.get(item_id, 0))
                comissao_unitaria = item.get('sale_fee', price * 0.16)
                imposto_reais = price * (imposto_padrao_pct / 100)
                
                if item_id not in agrupado:
                    agrupado[item_id] = {
                        'Produto': title, 'Giro': 0, 'Faturamento': 0, 'Ticket_Medio': price,
                        'Custo_CMV': custo_cmv, 'Custo_Frete': 0, 'Custo_Comissao': 0, 
                        'Custo_Imposto': 0, 'Custo_ADS': 0, 'Margem_Contribuicao': 0, 'Sem_Custo': (custo_cmv == 0)
                    }
                
                agrupado[item_id]['Giro'] += qty
                agrupado[item_id]['Faturamento'] += (price * qty)
                agrupado[item_id]['Custo_Frete'] += (custo_envio_unidade * qty)
                agrupado[item_id]['Custo_Comissao'] += (comissao_unitaria * qty)
                agrupado[item_id]['Custo_Imposto'] += (imposto_reais * qty)
                
                lucro_unidade = price - custo_cmv - custo_envio_unidade - comissao_unitaria - imposto_reais
                agrupado[item_id]['Margem_Contribuicao'] = lucro_unidade

        dados_reais = []
        for item_id, dados in agrupado.items():
            dados_reais.append({
                'ID': item_id,
                'Produto': dados['Produto'],
                'Ticket_Medio': dados['Ticket_Medio'],
                'Faturamento': dados['Faturamento'],
                'Giro': dados['Giro'],
                'Custo_Frete': dados['Custo_Frete'],
                'Custo_Comissao': dados['Custo_Comissao'],
                'Custo_Imposto': dados['Custo_Imposto'],
                'Custo_ADS': 0,
                'Margem_Contribuicao': dados['Margem_Contribuicao'],
                'Custo_CMV': dados['Custo_CMV'],
                'Sem_Custo': dados['Sem_Custo'],
                'Giro_Ant': int(dados['Giro'] * 0.9),
                'Status': "⚠️ Preencha o CMV" if dados['Sem_Custo'] else ("🔴 Erro de Preço" if dados['Margem_Contribuicao'] < 0 else "🟢 Saudável")
            })

        df = pd.DataFrame(dados_reais)
        
        # Cálculos de KPI
        lucro_total = float((df[~df['Sem_Custo']]['Giro'] * df[~df['Sem_Custo']]['Margem_Contribuicao']).sum())
        
        kpis = {
            "faturamento": f"R$ {float(df['Faturamento'].sum()):,.2f}".replace(',', 'X').replace('.', ',').replace('X', '.'),
            "lucro": f"R$ {lucro_total:,.2f}".replace(',', 'X').replace('.', ',').replace('X', '.'),
            "ads": "R$ 0,00",
            "unidades": str(int(df['Giro'].sum())),
            "alertas_criticos": int(len(df[df['Sem_Custo'] == True])),
            "periodo_nome": f"Últimos {periodo_dias} dias",
            "imposto_padrao": imposto_padrao_pct
        }

        return jsonify({"kpis": kpis, "tabela": df.to_dict(orient='records')})

    except Exception as e:
        return jsonify({"erro": str(e)}), 500
