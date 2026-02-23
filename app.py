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

# --- INICIALIZAÇÃO SEGURA DO FIREBASE ---
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
    return "Erro ao conectar com Mercado Livre."

# --- ROTAS DE CONFIGURAÇÃO (CUSTOS E IMPOSTOS) ---
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

# --- O MOTOR FINANCEIRO ABSOLUTO ---
@app.route('/api/dados')
def api_dados():
    uid = verificar_token(request)
    if not uid: return jsonify({"erro": "Acesso Negado."}), 401

    periodo_dias = int(request.args.get('periodo', 30))
    user_doc = db.collection('usuarios').document(uid).get().to_dict()
    if not user_doc or 'ml_access_token' not in user_doc: return jsonify({"erro": "ml_nao_conectado"}), 403

    ml_token = user_doc['ml_access_token']
    ml_seller_id = user_doc['ml_user_id']
    
    # 1. Busca Custos e Impostos no Firebase
    custos_db = db.collection('custos').document(uid).get().to_dict() or {}
    config_db = db.collection('configuracoes').document(uid).get().to_dict() or {}
    imposto_padrao_pct = float(config_db.get('imposto_padrao', 0))
    
    headers = {"Authorization": f"Bearer {ml_token}"}
    data_inicio = (datetime.utcnow() - timedelta(days=periodo_dias)).strftime('%Y-%m-%dT00:00:00.000-00:00')
    data_fim = datetime.utcnow().strftime('%Y-%m-%dT23:59:59.000-00:00')
    
    url_vendas = f"https://api.mercadolibre.com/orders/search?seller={ml_seller_id}&order.status=paid&order.date_created.from={data_inicio}&order.date_created.to={data_fim}"
    
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
                
                # EXTRAÇÃO INTELIGENTE DE COMISSÃO DA API (Se não vier na requisição atual, usa 16% de trava)
                comissao_unitaria = item.get('sale_fee', price * 0.16)
                
                imposto_reais = price * (imposto_padrao_pct / 100)
                custo_ads_unidade = 0 # Futuro: endpoint /advertising
                custo_devolucao_unidade = 0 # Futuro: endpoint /claims
                
                if item_id not in agrupado:
                    agrupado[item_id] = {
                        'Produto': title, 'Giro': 0, 'Faturamento': 0, 'Ticket_Medio': price,
                        'Custo_CMV': custo_cmv, 'Custo_Frete': 0, 'Custo_Comissao': 0, 
                        'Custo_Imposto': 0, 'Custo_ADS': 0, 'Custo_Devolucao': 0,
                        'Margem_Contribuicao': 0, 'Sem_Custo': (custo_cmv == 0)
                    }
                
                agrupado[item_id]['Giro'] += qty
                agrupado[item_id]['Faturamento'] += (price * qty)
                agrupado[item_id]['Custo_Frete'] += (custo_envio_unidade * qty)
                agrupado[item_id]['Custo_Comissao'] += (comissao_unitaria * qty)
                agrupado[item_id]['Custo_Imposto'] += (imposto_reais * qty)
                agrupado[item_id]['Custo_ADS'] += (custo_ads_unidade * qty)
                
                # MATEMÁTICA DEFINITIVA DE RENTABILIDADE (Por unidade)
                lucro_unidade = price - custo_cmv - custo_envio_unidade - comissao_unitaria - imposto_reais - custo_ads_unidade - custo_devolucao_unidade
                agrupado[item_id]['Margem_Contribuicao'] = lucro_unidade

        dados_reais = {
            'ID': [], 'Produto': [], 'Ticket_Medio': [], 'Faturamento': [], 'Giro': [], 
            'Margem_Contribuicao': [], 'Custo_CMV': [], 'Custo_ADS': [], 'Custo_Frete': [],
            'Custo_Comissao': [], 'Custo_Imposto': [], 'Sem_Custo': [], 'Giro_Ant': []
        }

        for item_id, dados in agrupado.items():
            dados_reais['ID'].append(item_id)
            dados_reais['Produto'].append(dados['Produto'])
            dados_reais['Ticket_Medio'].append(dados['Ticket_Medio'])
            dados_reais['Faturamento'].append(dados['Faturamento'])
            dados_reais['Giro'].append(dados['Giro'])
            dados_reais['Giro_Ant'].append(int(dados['Giro'] * 0.85)) # Simula mes anterior
            dados_reais['Custo_Frete'].append(dados['Custo_Frete'])
            dados_reais['Custo_Comissao'].append(dados['Custo_Comissao'])
            dados_reais['Custo_Imposto'].append(dados['Custo_Imposto'])
            dados_reais['Custo_ADS'].append(dados['Custo_ADS'])
            dados_reais['Margem_Contribuicao'].append(dados['Margem_Contribuicao'])
            dados_reais['Custo_CMV'].append(dados['Custo_CMV'])
            dados_reais['Sem_Custo'].append(dados['Sem_Custo'])

        df = pd.DataFrame(dados_reais)

    except Exception as e:
        print(f"Erro ao processar API: {e}")
        return jsonify({"erro": "Falha na leitura."}), 500

    def calc_trend(atual, anterior):
        if anterior == 0 and atual == 0: return 0
        if anterior == 0: return 100
        return round(((atual - anterior) / anterior) * 100, 1)

    df['Giro_Trend'] = df.apply(lambda x: calc_trend(x['Giro'], x['Giro_Ant']), axis=1)

    def gerar_status(row):
        if row['Sem_Custo']: return "⚠️ Preencha o CMV"
        mc = row['Margem_Contribuicao']
        ads = row['Custo_ADS']
        if mc < 0 and ads > 0: return "🔴 Pausar ADS"
        elif mc < 0 and ads <= 0: return "🔴 Erro de Preço"
        elif mc > 0 and ads > 0: return "🟢 Escalar ADS"
        else: return "🔵 Orgânico Saudável"
        
    df['Status'] = df.apply(gerar_status, axis=1)
    
    # PROMOÇÃO MÁXIMA ASSERTIVA (Lucro / Ticket)
    df['Desconto_Max'] = ((df['Margem_Contribuicao'] / df['Ticket_Medio']) * 100).round(2)
    df['Desconto_Max_Grafico'] = df['Desconto_Max'].apply(lambda x: x if x > 0 else 0)

    lucro_total = float((df[~df['Sem_Custo']]['Giro'] * df[~df['Sem_Custo']]['Margem_Contribuicao']).sum())

    kpis = {
        "faturamento": f"R$ {float(df['Faturamento'].sum()):,.2f}".replace(',', 'X').replace('.', ',').replace('X', '.'),
        "lucro": f"R$ {lucro_total:,.2f}".replace(',', 'X').replace('.', ',').replace('X', '.'),
        "ads": f"R$ {float(df['Custo_ADS'].sum()):,.2f}".replace(',', 'X').replace('.', ',').replace('X', '.'),
        "unidades": str(int(df['Giro'].sum())),
        "alertas_criticos": int(len(df[(df['Margem_Contribuicao'] < 0) | (df['Sem_Custo'] == True)])),
        "periodo_nome": f"Últimos {periodo_dias} dias",
        "imposto_padrao": imposto_padrao_pct
    }

    return jsonify({"kpis": kpis, "tabela": df.to_dict(orient='records')})
