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
ML_APP_ID = "1096855357952882"
ML_SECRET_KEY = "vzOhLT31AxYEqS4JJ9qfuoYGZtsbg1AM"
REDIRECT_URI = "https://irving-blm.vercel.app/callback" # Mude para a sua URL real

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
        
        if not resultados:
            # Retorna uma tabela vazia se não houver vendas no período para não quebrar o layout
            return jsonify({"kpis": {"faturamento": "R$ 0,00", "lucro": "R$ 0,00", "ads": "R$ 0,00", "unidades": "0", "alertas_criticos": 0, "periodo_nome": f"Últimos {periodo_dias} dias"}, "tabela": []})

        # --- PROCESSAMENTO DOS DADOS REAIS (O "Motor" de Agrupamento) ---
        agrupado = {}
        for order in resultados:
            # Identifica o país de destino para aplicar a regra de custo logístico
            destino = order.get('shipping', {}).get('receiver_address', {}).get('country', {}).get('id', 'BR')
            # Custo de envio estimado (zera se for Argentina)
            custo_envio = 0 if destino == 'AR' else 18.00 
            
            for item in order.get('order_items', []):
                item_id = item['item']['id']
                title = item['item']['title']
                qty = item['quantity']
                price = item['unit_price']
                
                if item_id not in agrupado:
                    agrupado[item_id] = {
                        'Produto': title,
                        'Giro': 0,
                        'Ticket_Medio': price,
                        'Investimento_ADS': 0, # Requer integração com a API de Advertising futuramente
                        'Custo_Envio_Total': 0,
                        'Margem_Contribuicao': 0
                    }
                
                agrupado[item_id]['Giro'] += qty
                agrupado[item_id]['Custo_Envio_Total'] += (custo_envio * qty)
                
                # Simulação de Custo do Produto (CMV) em 40% do valor de venda para não quebrar a matemática.
                # O próximo passo será puxar esse custo real do Firebase!
                custo_produto = price * 0.40
                lucro_unitario = price - custo_produto - custo_envio
                agrupado[item_id]['Margem_Contribuicao'] = lucro_unitario

        # Transforma o dicionário agrupado nas listas para o Pandas
        dados_reais = {
            'ID': [], 'Produto': [], 'Ticket_Medio': [], 'Giro': [], 
            'Giro_Ant': [], 'Investimento_ADS': [], 'Investimento_ADS_Ant': [],
            'Margem_Contribuicao': [], 'Margem_Contribuicao_Ant': []
        }

        for item_id, dados in agrupado.items():
            dados_reais['ID'].append(item_id)
            dados_reais['Produto'].append(dados['Produto'])
            dados_reais['Ticket_Medio'].append(dados['Ticket_Medio'])
            dados_reais['Giro'].append(dados['Giro'])
            dados_reais['Giro_Ant'].append(int(dados['Giro'] * 0.85)) # Simula dados do mês passado
            dados_reais['Investimento_ADS'].append(dados['Investimento_ADS'])
            dados_reais['Investimento_ADS_Ant'].append(0)
            dados_reais['Margem_Contribuicao'].append(dados['Margem_Contribuicao'])
            dados_reais['Margem_Contribuicao_Ant'].append(dados['Margem_Contribuicao'] * 0.9)

        df = pd.DataFrame(dados_reais)

    except Exception as e:
        print(f"Erro ao processar API do ML: {e}")
        return jsonify({"erro": "Falha na leitura dos dados."}), 500

    # --- CÁLCULOS DE TENDÊNCIA E STATUS ---
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

    # Consolida os KPIs
    kpis = {
        "faturamento": f"R$ {float((df['Giro'] * df['Ticket_Medio']).sum()):,.2f}".replace(',', 'X').replace('.', ',').replace('X', '.'),
        "lucro": f"R$ {float((df['Giro'] * df['Margem_Contribuicao']).sum()):,.2f}".replace(',', 'X').replace('.', ',').replace('X', '.'),
        "ads": f"R$ {float(df['Investimento_ADS'].sum()):,.2f}".replace(',', 'X').replace('.', ',').replace('X', '.'),
        "unidades": str(int(df['Giro'].sum())),
        "alertas_criticos": int(len(df[df['Margem_Contribuicao'] < 0])),
        "periodo_nome": f"Últimos {periodo_dias} dias"
    }

    return jsonify({"kpis": kpis, "tabela": df.to_dict(orient='records')})
