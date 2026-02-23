from flask import Flask, render_template, request, jsonify, redirect
import firebase_admin
from firebase_admin import credentials, firestore
import requests
import os

app = Flask(__name__)

# 1. INICIALIZA O FIREBASE (Banco de Dados)
# Você vai colocar aquele arquivo .json que baixou na mesma pasta do projeto
cred = credentials.Certificate("firebase-chave.json")
firebase_admin.initialize_app(cred)
db = firestore.client()

# Credenciais do Mercado Livre (Pegue no Dev Center do ML)
ML_APP_ID = "SEU_APP_ID"
ML_SECRET_KEY = "SUA_SECRET_KEY"
REDIRECT_URI = "https://irving-app.onrender.com/callback" # URL que teremos após subir pro ar

@app.route('/')
def home():
    return render_template('index.html')

# ---------------------------------------------------------
# ROTA 1: O VENDEDOR AUTORIZA O IRVING NO MERCADO LIVRE
# ---------------------------------------------------------
@app.route('/conectar-ml/<user_id>')
def conectar_ml(user_id):
    # Redireciona o cliente para a tela oficial de permissão do ML
    url_auth = f"https://auth.mercadolivre.com.br/authorization?response_type=code&client_id={ML_APP_ID}&redirect_uri={REDIRECT_URI}&state={user_id}"
    return redirect(url_auth)

# ---------------------------------------------------------
# ROTA 2: O RETORNO DO MERCADO LIVRE (Salvando o Token)
# ---------------------------------------------------------
@app.route('/callback')
def callback():
    # O ML devolve um código e o ID do seu usuário (state)
    code = request.args.get('code')
    user_id = request.args.get('state')
    
    # Trocamos o código pelo Access Token real
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
        # Salva o token do cliente no Firebase!
        db.collection('usuarios').document(user_id).set({
            'ml_access_token': resposta['access_token'],
            'ml_refresh_token': resposta['refresh_token'],
            'status': 'conectado'
        }, merge=True)
        
        return "Conexão com Mercado Livre realizada com sucesso! Volte para o painel."
    return "Erro ao conectar."

# ---------------------------------------------------------
# ROTA 3: PUXANDO VENDAS E CUSTOS REAIS (A Mágica)
# ---------------------------------------------------------
@app.route('/api/dados')
def api_dados():
    user_id = request.args.get('user_id')
    
    # 1. Pega o token do vendedor no Banco de Dados
    user_doc = db.collection('usuarios').document(user_id).get()
    if not user_doc.exists:
        return jsonify({"erro": "Usuário não encontrado"}), 404
        
    token = user_doc.to_dict().get('ml_access_token')
    
    # 2. Puxa as vendas da API do ML (Exemplo)
    headers = {"Authorization": f"Bearer {token}"}
    vendas = requests.get(f"https://api.mercadolibre.com/orders/search?seller={user_id}&order.status=paid", headers=headers).json()
    
    # 3. Puxa os custos do produto cadastrados pelo vendedor no Firebase
    custos_db = db.collection('custos_produtos').document(user_id).get().to_dict() or {}
    
    # AQUI ENTRA O SEU MOTOR LOGICO DO PANDAS (Exatamente como estava antes)
    # Você processará 'vendas' e 'custos_db' usando as regras estabelecidas.
    
    # Nota de Segurança do Sistema:
    # Lembre-se de implementar o bloqueio nas rotas que respondem perguntas de compradores.
    # Qualquer resposta gerada que contenha "http" ou "www" deve ser barrada antes de ir para a API do ML
    # para evitar a suspensão da conta do vendedor.
    
    return jsonify({"status": "Dados processados"})

if __name__ == '__main__':
    # A porta é definida pelo servidor (Render) quando for pro ar
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)