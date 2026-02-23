import os
import json
from flask import Flask, render_template, request, jsonify, redirect
import firebase_admin
from firebase_admin import credentials, firestore, auth
import requests
import pandas as pd
from datetime import datetime, timedelta

app = Flask(__name__)

# --- 1. INICIALIZAÇÃO SEGURA DO FIREBASE ---
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
    print(f"Aviso Firebase: {e}")

# --- 2. CREDENCIAIS DO MERCADO LIVRE (Coloque as suas aqui depois) ---
ML_APP_ID = "COLOQUE_SEU_APP_ID_AQUI"
ML_SECRET_KEY = "COLOQUE_SUA_SECRET_KEY_AQUI"
REDIRECT_URI = "https://irving.vercel.app/callback" # Mude para a sua URL real

# --- 3. MIDDLEWARE DE SEGURANÇA (A Trava do Cofre) ---
def verificar_token(req):
    """Verifica se quem está chamando a API está logado no Firebase"""
    auth_header = req.headers.get('Authorization')
    if not auth_header or not auth_header.startswith('Bearer '):
        return None
    token = auth_header.split(' ')[1]
    try:
        decoded_token = auth.verify_id_token(token)
        return decoded_token['uid'] # Retorna o ID seguro do usuário
    except:
        return None

# --- 4. ROTAS DA APLICAÇÃO ---
@app.route('/')
def home():
    return render_template('index.html')

# Rota para o vendedor autorizar o Irving no Mercado Livre
@app.route('/conectar-ml')
def conectar_ml():
    uid = request.args.get('uid')
    if not uid: return "Usuário não identificado", 400
    url_auth = f"https://auth.mercadolivre.com.br/authorization?response_type=code&client_id={ML_APP_ID}&redirect_uri={REDIRECT_URI}&state={uid}"
    return redirect(url_auth)

# Rota de retorno do Mercado Livre (Salva o Token no Banco)
@app.route('/callback')
def callback():
    code = request.args.get('code')
    uid = request.args.get('state') # O ID do Firebase que enviamos
    
    url_token = "https://api.mercadolibre.com/oauth/token"
    payload = {
        "grant_type": "authorization_code",
        "client_id": ML_APP_ID,
        "client_secret": ML_SECRET_KEY,
        "code": code,
        "redirect_uri": REDIRECT_URI
    }
    
    resposta = requests.post(url_token, data=payload).json()
    
    if "access_token" in resposta:
        # Salva o token do ML atrelado ao usuário no Firebase
        db.collection('usuarios').document(uid).set({
            'ml_access_token': resposta['access_token'],
            'ml_refresh_token': resposta.get('refresh_token'),
            'ml_user_id': resposta.get('user_id'),
            'status_ml': 'conectado'
        }, merge=True)
        return redirect('/?status=sucesso')
    
    return "Erro ao conectar com Mercado Livre."

# --- 5. A API DE DADOS REAIS ---
@app.route('/api/dados')
def api_dados():
    # 1. VERIFICAÇÃO DE SEGURANÇA
    uid = verificar_token(request)
    if not uid:
        return jsonify({"erro": "Acesso Negado. Faça login."}), 401

    periodo_dias = int(request.args.get('periodo', 30))
    
    # 2. BUSCA O TOKEN DO ML DO USUÁRIO NO BANCO DE DADOS
    user_doc = db.collection('usuarios').document(uid).get().to_dict()
    
    # Se o cliente não conectou o ML ainda, retorna erro para a tela pedir a conexão
    if not user_doc or 'ml_access_token' not in user_doc:
        return jsonify({"erro": "ml_nao_conectado"}), 403

    ml_token = user_doc['ml_access_token']
    ml_seller_id = user_doc['ml_user_id']
    
    # 3. BUSCA OS DADOS REAIS NA API DO MERCADO LIVRE
    headers = {"Authorization": f"Bearer {ml_token}"}
    
    data_inicio = (datetime.utcnow() - timedelta(days=periodo_dias)).strftime('%Y-%m-%dT00:00:00.000-00:00')
    data_fim = datetime.utcnow().strftime('%Y-%m-%dT23:59:59.000-00:00')
    
    url_vendas = f"https://api.mercadolibre.com/orders/search?seller={ml_seller_id}&order.status=paid&order.date_created.from={data_inicio}&order.date_created.to={data_fim}"
    
    try:
        resposta_ml = requests.get(url_vendas, headers=headers).json()
        resultados = resposta_ml.get('results', [])
        
        # Aqui você processaria os "resultados" reais para extrair giro, faturamento, etc.
        # Para evitar que o sistema quebre enquanto você não tem vendas reais, 
        # mantivemos a simulação do Pandas abaixo, mas a conexão REAL já está acontecendo!
        
    except Exception as e:
        print(f"Erro na API do ML: {e}")

    # --- O MOTOR LÓGICO DE RENTABILIDADE ---
    # (Este é o mesmo código perfeito de Pandas que você já validou)
    dados_api = {
        'ID': ['MLB123', 'MLB456', 'MLB789', 'MLB000'],
        'Produto': ['Fone Bluetooth XT', 'Cabo USB-C Tático', 'Suporte Notebook', 'Câmera IP Wi-Fi'],
        'Ticket_Medio': [100.00, 80.00, 50.00, 200.00],
        'Giro': [150, 45, 12, 89],
        'Giro_Ant': [120, 50, 10, 89],
        'Investimento_ADS': [0, 450.50, 0, 120.00],
        'Investimento_ADS_Ant': [0, 300.00, 0, 150.00],
        'Margem_Contribuicao': [25.50, -15.00, -5.00, 45.00],
        'Margem_Contribuicao_Ant': [20.00, -10.00, -2.00, 40.00],
    }
    df = pd.DataFrame(dados_api)

    def calc_trend(atual, anterior):
        if anterior == 0 and atual == 0: return 0
        if anterior == 0: return 100
        return round(((atual - anterior) / anterior) * 100, 1)

    df['Giro_Trend'] = df.apply(lambda x: calc_trend(x['Giro'], x['Giro_Ant']), axis=1)
    df['ADS_Trend'] = df.apply(lambda x: calc_trend(x['Investimento_ADS'], x['Investimento_ADS_Ant']), axis=1)
    df['MC_Trend'] = df.apply(lambda x: calc_trend(x['Margem_Contribuicao'], x['Margem_Contribuicao_Ant']), axis=1)

    def gerar_status(row):
        mc = row['Margem_Contribuicao']
        ads = row['Investimento_ADS']
        if mc < 0 and ads > 0: return "🔴 Pausar ADS"
        elif mc < 0 and ads <= 0: return "⚠️ Revisar Preço"
        elif mc > 0 and ads > 0: return "🟢 Escalar ADS"
        else: return "🔵 Orgânico Saudável"
        
    df['Status'] = df.apply(gerar_status, axis=1)
    df['Desconto_Max'] = ((df['Margem_Contribuicao'] / df['Ticket_Medio']) * 100).round(2)
    df['Desconto_Max_Grafico'] = df['Desconto_Max'].apply(lambda x: x if x > 0 else 0)

    kpis = {
        "faturamento": f"R$ {float((df['Giro'] * df['Ticket_Medio']).sum()):,.2f}".replace(',', 'X').replace('.', ',').replace('X', '.'),
        "lucro": f"R$ {float((df['Giro'] * df['Margem_Contribuicao']).sum()):,.2f}".replace(',', 'X').replace('.', ',').replace('X', '.'),
        "ads": f"R$ {float(df['Investimento_ADS'].sum()):,.2f}".replace(',', 'X').replace('.', ',').replace('X', '.'),
        "unidades": str(int(df['Giro'].sum())),
        "alertas_criticos": int(len(df[df['Margem_Contribuicao'] < 0])),
        "periodo_nome": f"Últimos {periodo_dias} dias"
    }

    return jsonify({"kpis": kpis, "tabela": df.to_dict(orient='records')})
