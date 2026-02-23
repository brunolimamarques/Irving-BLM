import os
import json
from flask import Flask, render_template, request, jsonify
import firebase_admin
from firebase_admin import credentials, firestore
import pandas as pd

app = Flask(__name__)

# --- 1. CONFIGURAÇÃO DE SEGURANÇA DO FIREBASE ---
firebase_cred_string = os.environ.get("FIREBASE_JSON")

try:
    if firebase_cred_string:
        # Lê a chave secreta direto do cofre da Vercel
        cred_dict = json.loads(firebase_cred_string)
        cred = credentials.Certificate(cred_dict)
    else:
        # Se você for testar no seu PC, ele procura o arquivo físico (Opcional)
        cred = credentials.Certificate("firebase-chave.json")
        
    # Inicializa sem duplicar
    if not firebase_admin._apps:
        firebase_admin.initialize_app(cred)
        
    db = firestore.client()
except Exception as e:
    print(f"Aviso: Não foi possível conectar ao Firebase no momento. {e}")

# --- 2. ROTAS DA APLICAÇÃO ---
@app.route('/')
def home():
    return render_template('index.html')

@app.route('/api/dados')
def api_dados():
    periodo = request.args.get('periodo', '30')
    
    # Simulação dos dados que virão da API do Mercado Livre
    dados_api = {
        'ID': ['MLB123', 'MLB456', 'MLB789', 'MLB000', 'MLB555'],
        'Produto': ['Fone Bluetooth XT', 'Cabo USB-C Tático', 'Suporte Notebook', 'Câmera IP Wi-Fi', 'Kit Ferramentas'],
        'Ticket_Medio': [100.00, 80.00, 50.00, 200.00, 85.00],
        'Giro': [150, 45, 12, 89, 210],
        'Giro_Ant': [120, 50, 10, 89, 150],
        'Investimento_ADS': [0, 450.50, 0, 120.00, 50.00],
        'Investimento_ADS_Ant': [0, 300.00, 0, 150.00, 40.00],
        'Margem_Contribuicao': [25.50, -15.00, -5.00, 45.00, 18.00],
        'Margem_Contribuicao_Ant': [20.00, -10.00, -2.00, 40.00, 20.00],
    }
    df = pd.DataFrame(dados_api)

    # Motor de Tendência e Regras
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

    # KPIs Totais
    kpis = {
        "faturamento": f"R$ {float((df['Giro'] * df['Ticket_Medio']).sum()):,.2f}".replace(',', 'X').replace('.', ',').replace('X', '.'),
        "lucro": f"R$ {float((df['Giro'] * df['Margem_Contribuicao']).sum()):,.2f}".replace(',', 'X').replace('.', ',').replace('X', '.'),
        "ads": f"R$ {float(df['Investimento_ADS'].sum()):,.2f}".replace(',', 'X').replace('.', ',').replace('X', '.'),
        "unidades": str(int(df['Giro'].sum())),
        "alertas_criticos": int(len(df[df['Margem_Contribuicao'] < 0])),
        "periodo_nome": f"Últimos {periodo} dias" if periodo != "custom" else "Personalizado"
    }

    return jsonify({"kpis": kpis, "tabela": df.to_dict(orient='records')})

# (Na Vercel, o 'app.run' não é necessário, o servidor gerencia isso)