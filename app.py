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

# --- 1. CONFIGURAÇÕES PRINCIPAIS ---
ADMIN_EMAIL = "brunolima.marques@gmail.com" # <--- COLOQUE AQUI O SEU E-MAIL DE LOGIN MASTER
ML_APP_ID = "1096855357952882"
ML_SECRET_KEY = "vzOhLT31AxYEqS4JJ9qfuoYGZtsbg1AM"
REDIRECT_URI = "https://irving-blm.vercel.app/callback"

# --- 2. INICIALIZAÇÃO FIREBASE ---
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

def verificar_token(req):
    auth_header = req.headers.get('Authorization')
    if not auth_header or not auth_header.startswith('Bearer '): return None
    try: return auth.verify_id_token(auth_header.split(' ')[1])['uid']
    except: return None

# --- 3. AUTO-REFRESH DO TOKEN ML ---
def gerenciar_token_ml(uid, user_doc):
    ml_token = user_doc.get('ml_access_token')
    refresh_token = user_doc.get('ml_refresh_token')
    
    teste_url = "https://api.mercadolibre.com/users/me"
    res = requests.get(teste_url, headers={"Authorization": f"Bearer {ml_token}"})
    
    if res.status_code == 401: 
        print(f"Token expirado para {uid}. A renovar...")
        url_refresh = "https://api.mercadolibre.com/oauth/token"
        payload = {"grant_type": "refresh_token", "client_id": ML_APP_ID, "client_secret": ML_SECRET_KEY, "refresh_token": refresh_token}
        refresh_res = requests.post(url_refresh, data=payload).json()
        
        if "access_token" in refresh_res:
            ml_token = refresh_res['access_token']
            db.collection('usuarios').document(uid).update({
                'ml_access_token': ml_token,
                'ml_refresh_token': refresh_res.get('refresh_token', refresh_token)
            })
    return ml_token

# --- 4. ROTAS BÁSICAS E ADMIN ---
@app.route('/')
def home(): return render_template('index.html')

@app.route('/conectar-ml')
def conectar_ml():
    uid = request.args.get('uid')
    if not uid: return "Utilizador não identificado", 400
    parametros = {"response_type": "code", "client_id": ML_APP_ID, "redirect_uri": REDIRECT_URI, "state": uid, "prompt": "consent"}
    return redirect("https://auth.mercadolivre.com.br/authorization?" + urllib.parse.urlencode(parametros))

@app.route('/callback')
def callback():
    code = request.args.get('code')
    uid = request.args.get('state')
    payload = {"grant_type": "authorization_code", "client_id": ML_APP_ID, "client_secret": ML_SECRET_KEY, "code": code, "redirect_uri": REDIRECT_URI}
    resposta = requests.post("https://api.mercadolibre.com/oauth/token", data=payload).json()
    if "access_token" in resposta:
        db.collection('usuarios').document(uid).set({
            'ml_access_token': resposta['access_token'], 'ml_refresh_token': resposta.get('refresh_token'),
            'ml_user_id': resposta.get('user_id'), 'status_ml': 'conectado'
        }, merge=True)
        return redirect('/?status=sucesso')
    return "Erro ao conectar."

@app.route('/api/clientes')
def api_clientes():
    uid = verificar_token(request)
    if not uid: return jsonify({"erro": "Acesso Negado."}), 401
    try:
        if auth.get_user(uid).email != ADMIN_EMAIL: return jsonify({"erro": "Acesso Negado. Não é Admin."}), 403
    except: return jsonify({"erro": "Erro auth"}), 403
        
    clientes = []
    for doc in db.collection('usuarios').stream():
        try:
            u = auth.get_user(doc.id)
            if u.email != ADMIN_EMAIL: clientes.append({"uid": doc.id, "email": u.email})
        except: pass
    return jsonify(clientes)

@app.route('/api/salvar_custo', methods=['POST'])
def salvar_custo():
    uid = verificar_token(request)
    if not uid: return jsonify({"erro": "Acesso Negado"}), 401
    client_uid = request.json.get('client_uid')
    if client_uid:
        try:
            if auth.get_user(uid).email == ADMIN_EMAIL: uid = client_uid
        except: pass
    db.collection('custos').document(uid).set({request.json.get('item_id'): float(request.json.get('custo', 0))}, merge=True)
    return jsonify({"status": "sucesso"})

@app.route('/api/salvar_custos_massa', methods=['POST'])
def salvar_custos_massa():
    uid = verificar_token(request)
    if not uid: return jsonify({"erro": "Acesso Negado"}), 401
    client_uid = request.json.get('client_uid')
    if client_uid:
        try:
            if auth.get_user(uid).email == ADMIN_EMAIL: uid = client_uid
        except: pass
    custos_novos = request.json.get('custos', {})
    custos_formatados = {k: float(v) for k, v in custos_novos.items()}
    db.collection('custos').document(uid).set(custos_formatados, merge=True)
    return jsonify({"status": "sucesso"})

@app.route('/api/salvar_imposto', methods=['POST'])
def salvar_imposto():
    uid = verificar_token(request)
    if not uid: return jsonify({"erro": "Acesso Negado"}), 401
    client_uid = request.json.get('client_uid')
    if client_uid:
        try:
            if auth.get_user(uid).email == ADMIN_EMAIL: uid = client_uid
        except: pass
    db.collection('configuracoes').document(uid).set({'imposto_padrao': float(request.json.get('imposto', 0))}, merge=True)
    return jsonify({"status": "sucesso"})

# --- 5. MOTOR FINANCEIRO ---
@app.route('/api/dados')
def api_dados():
    uid = verificar_token(request)
    if not uid: return jsonify({"erro": "Acesso Negado."}), 401

    client_uid = request.args.get('client_uid')
    if client_uid:
        try:
            if auth.get_user(uid).email == ADMIN_EMAIL: uid = client_uid
        except: pass

    periodo_dias = int(request.args.get('periodo', 30))
    user_doc = db.collection('usuarios').document(uid).get().to_dict()
    if not user_doc or 'ml_access_token' not in user_doc: return jsonify({"erro": "ml_nao_conectado"}), 403

    ml_token = gerenciar_token_ml(uid, user_doc)
    ml_seller_id = user_doc['ml_user_id']
    
    custos_db = db.collection('custos').document(uid).get().to_dict() or {}
    config_db = db.collection('configuracoes').document(uid).get().to_dict() or {}
    imposto_padrao_pct = float(config_db.get('imposto_padrao', 0))
    
    headers = {"Authorization": f"Bearer {ml_token}"}
    data_inicio_obj = datetime.utcnow() - timedelta(days=periodo_dias)
    data_inicio = data_inicio_obj.strftime('%Y-%m-%dT00:00:00.000-00:00')
    data_fim_str = datetime.utcnow().strftime('%Y-%m-%d')
    data_inicio_str = data_inicio_obj.strftime('%Y-%m-%d')
    
    url_vendas = f"https://api.mercadolibre.com/orders/search?seller={ml_seller_id}&order.date_created.from={data_inicio}"
    
    try:
        resposta_ml = requests.get(url_vendas, headers=headers).json()
        resultados = resposta_ml.get('results', [])
        
        if not resultados: return jsonify({"erro": "vazio", "imposto_padrao": imposto_padrao_pct})

        # --- BUSCA EXATA DOS FRETES ---
        shipping_ids = [str(o.get('shipping', {}).get('id')) for o in resultados if o.get('shipping', {}).get('id')]
        custos_frete_reais = {}
        if shipping_ids:
            shipping_ids = list(set(shipping_ids))
            for i in range(0, len(shipping_ids), 50):
                lote = shipping_ids[i:i+50]
                ids_str = ",".join(lote)
                url_ship = f"https://api.mercadolibre.com/shipments?ids={ids_str}"
                try:
                    res_ship = requests.get(url_ship, headers=headers).json()
                    for ship_info in res_ship:
                        if ship_info.get('code') == 200:
                            ship_body = ship_info.get('body', {})
                            s_id = str(ship_body.get('id'))
                            base = float(ship_body.get('base_cost', 0) or 0)
                            buyer_pays = float(ship_body.get('shipping_option', {}).get('cost', 0) or 0)
                            custos_frete_reais[s_id] = max(0, base - buyer_pays)
                except Exception as e:
                    print("Erro Frete:", e)

        agrupado = {}
        timeline = {}

        for order in resultados:
            data_venda = order.get('date_created', '')[:10]
            if data_venda not in timeline: timeline[data_venda] = {'faturamento': 0, 'lucro': 0}
            
            ship_id = str(order.get('shipping', {}).get('id', ''))
            frete_total_pedido = custos_frete_reais.get(ship_id, 0)
            
            order_items = order.get('order_items', [])
            total_qty_order = sum(item['quantity'] for item in order_items)
            custo_envio_unidade = frete_total_pedido / total_qty_order if total_qty_order > 0 else 0
            
            for item in order_items:
                item_id = item['item']['id']
                title = item['item']['title']
                qty = item['quantity']
                price = float(item['unit_price'])
                
                custo_cmv = float(custos_db.get(item_id, 0))
                
                # --- CORREÇÃO DO CUSTO FIXO E COMISSÃO ---
                comissao_unitaria = float(item.get('sale_fee') or (price * 0.16))
                
                # O ML cobra R$ 6,00 fixos para anúncios abaixo de R$ 79,00
                custo_fixo_unidade = 6.00 if price < 79 else 0.00
                
                imposto_reais = price * (imposto_padrao_pct / 100)
                
                # Lucro abate TODOS os custos (CMV, Frete Exato, Comissão %, Custo Fixo de R$ 6,00 e Imposto)
                lucro_unidade_base = price - custo_cmv - custo_envio_unidade - comissao_unitaria - custo_fixo_unidade - imposto_reais
                
                timeline[data_venda]['faturamento'] += (price * qty)
                if custo_cmv > 0: timeline[data_venda]['lucro'] += (lucro_unidade_base * qty)

                if item_id not in agrupado:
                    agrupado[item_id] = {
                        'Produto': title, 'Giro': 0, 'Faturamento': 0, 'Ticket_Medio': price,
                        'Custo_CMV': custo_cmv, 'Custo_Frete': 0, 'Custo_Comissao': 0, 'Custo_Fixo': 0,
                        'Custo_Imposto': 0, 'Custo_ADS': 0, 'Margem_Contribuicao': 0, 'Sem_Custo': (custo_cmv == 0)
                    }
                
                agrupado[item_id]['Giro'] += qty
                agrupado[item_id]['Faturamento'] += (price * qty)
                agrupado[item_id]['Custo_Frete'] += (custo_envio_unidade * qty)
                agrupado[item_id]['Custo_Comissao'] += (comissao_unitaria * qty)
                agrupado[item_id]['Custo_Fixo'] += (custo_fixo_unidade * qty) # Sobe para a interface
                agrupado[item_id]['Custo_Imposto'] += (imposto_reais * qty)
                agrupado[item_id]['Margem_Contribuicao'] += (lucro_unidade_base * qty) 

        # Processamento do ADS
        item_ids_list = list(agrupado.keys())
        if item_ids_list:
            for i in range(0, len(item_ids_list), 50):
                lote_ids = item_ids_list[i:i+50]
                ids_str = ",".join(lote_ids)
                url_ads = f"https://api.mercadolibre.com/advertising/product_ads/metrics/items?date_from={data_inicio_str}&date_to={data_fim_str}&item_ids={ids_str}"
                try:
                    res_ads = requests.get(url_ads, headers=headers)
                    if res_ads.status_code == 200:
                        for ad_metric in res_ads.json():
                            id_anuncio = ad_metric.get('item_id')
                            custo_ads = float(ad_metric.get('metrics', {}).get('cost', 0))
                            if id_anuncio in agrupado: agrupado[id_anuncio]['Custo_ADS'] = custo_ads
                except Exception as e: print(f"Aviso ADS: {e}")

        dados_reais = []
        for item_id, dados in agrupado.items():
            giro = dados['Giro']
            custo_ads_total = dados['Custo_ADS']
            lucro_total_liquido = dados['Margem_Contribuicao'] - custo_ads_total
            margem_unitaria_final = lucro_total_liquido / giro if giro > 0 else 0

            dados_reais.append({
                'ID': item_id, 'Produto': dados['Produto'], 'Ticket_Medio': dados['Ticket_Medio'],
                'Faturamento': dados['Faturamento'], 'Giro': giro, 'Custo_Frete': dados['Custo_Frete'],
                'Custo_Comissao': dados['Custo_Comissao'], 'Custo_Fixo': dados['Custo_Fixo'], 
                'Custo_Imposto': dados['Custo_Imposto'], 'Custo_ADS': custo_ads_total, 
                'Custo_CMV': dados['Custo_CMV'], 'Sem_Custo': dados['Sem_Custo'],
                'Margem_Contribuicao': margem_unitaria_final, 
                'Giro_Ant': int(giro * 0.85), 'Margem_Ant': margem_unitaria_final * 0.9 
            })

        df = pd.DataFrame(dados_reais)
        
        def calc_trend(atual, anterior):
            if anterior == 0 and atual == 0: return 0
            if anterior == 0: return 100
            return round(((atual - anterior) / anterior) * 100, 1)

        df['Giro_Trend'] = df.apply(lambda x: calc_trend(x['Giro'], x['Giro_Ant']), axis=1)
        df['MC_Trend'] = df.apply(lambda x: calc_trend(x['Margem_Contribuicao'], x['Margem_Ant']), axis=1)

        def gerar_status(row):
            if row['Sem_Custo']: return "⚠️ Preencha o CMV"
            mc = row['Margem_Contribuicao']
            if mc < 0: return "🔴 Erro de Preço"
            elif mc > 0 and row['Custo_ADS'] > 0: return "🟢 Escalar ADS"
            else: return "🔵 Saudável"
            
        df['Status'] = df.apply(gerar_status, axis=1)
        df['Desconto_Max'] = (((df['Margem_Contribuicao'] - (df['Ticket_Medio'] * 0.15)) / df['Ticket_Medio']) * 100).round(2)
        df['Desconto_Max_Grafico'] = df['Desconto_Max'].apply(lambda x: x if x > 0 else 0)

        lucro_total = float((df[~df['Sem_Custo']]['Giro'] * df[~df['Sem_Custo']]['Margem_Contribuicao']).sum())
        ads_total = float(df['Custo_ADS'].sum())
        faturamento_total = float(df['Faturamento'].sum())
        
        timeline_ordenada = dict(sorted(timeline.items()))
        grafico_dados = { "labels": list(timeline_ordenada.keys()), "faturamento": [v['faturamento'] for v in timeline_ordenada.values()], "lucro": [v['lucro'] for v in timeline_ordenada.values()] }

        # --- PROCESSAMENTO: CURVA ABC NO BACKEND ---
        df_abc = df[df['Faturamento'] > 0].sort_values(by='Faturamento', ascending=False)
        faturamento_acumulado = 0
        curva_abc = []
        for _, row in df_abc.iterrows():
            faturamento_acumulado += row['Faturamento']
            pct_acumulada = (faturamento_acumulado / faturamento_total) * 100 if faturamento_total > 0 else 0
            pct_total = (row['Faturamento'] / faturamento_total) * 100 if faturamento_total > 0 else 0
            classe = 'A' if pct_acumulada <= 80 else ('B' if pct_acumulada <= 95 else 'C')
            curva_abc.append({
                'ID': row['ID'], 'Produto': row['Produto'], 'Faturamento': row['Faturamento'], 
                'Percentual': round(pct_total, 1), 'Classe': classe
            })

        # --- PROCESSAMENTO: DIAGNÓSTICO DO PREÇO NO BACKEND ---
        diagnosticos = []
        for _, row in df.iterrows():
            if row['Sem_Custo'] or row['Giro'] == 0: continue
            margem_pct = (row['Margem_Contribuicao'] / row['Ticket_Medio']) * 100 if row['Ticket_Medio'] > 0 else 0
            
            if margem_pct > 30 and row['Giro'] < 5:
                diagnosticos.append({
                    'tipo': 'escala', 'titulo': 'Potencial de Escala', 'produto': row['Produto'],
                    'mensagem': f"A sua Margem de Contribuição está alta ({margem_pct:.1f}%), mas vendeu apenas {row['Giro']} unidades. Teste reduzir o preço em 5% a 10% para ganhar tração e aumentar o Lucro Bruto total."
                })
            elif 0 < margem_pct < 15 and row['Custo_ADS'] > 0:
                diagnosticos.append({
                    'tipo': 'alerta_ads', 'titulo': 'Alerta de ADS', 'produto': row['Produto'],
                    'mensagem': f"A sua Margem de Contribuição é de apenas {margem_pct:.1f}% e o ADS está ativo. Vigie de perto para garantir que o ACOS não consome todo o seu Lucro Bruto."
                })

        # --- BUSCA DO ESTOQUE PARADO ---
        url_itens_ativos = f"https://api.mercadolibre.com/users/{ml_seller_id}/items/search?status=active&limit=50"
        estoque_parado = []
        try:
            res_itens = requests.get(url_itens_ativos, headers=headers).json()
            todos_itens = res_itens.get('results', [])
            itens_parados_ids = [i for i in todos_itens if i not in agrupado]
            if itens_parados_ids:
                for i in range(0, len(itens_parados_ids), 20):
                    lote = itens_parados_ids[i:i+20]
                    ids_str = ",".join(lote)
                    url_detalhes = f"https://api.mercadolibre.com/items?ids={ids_str}"
                    res_detalhes = requests.get(url_detalhes, headers=headers).json()
                    for item_obj in res_detalhes:
                        if item_obj.get('code') == 200:
                            body = item_obj['body']
                            estoque_parado.append({
                                'ID': body.get('id'), 'Produto': body.get('title'),
                                'Preco': body.get('price'), 'Disponivel': body.get('available_quantity', 0),
                                'Link': body.get('permalink', '#')
                            })
        except Exception as e:
            print("Aviso Estoque Parado:", e)

        kpis = {
            "faturamento": f"R$ {faturamento_total:,.2f}".replace(',', 'X').replace('.', ',').replace('X', '.'),
            "lucro": f"R$ {lucro_total:,.2f}".replace(',', 'X').replace('.', ',').replace('X', '.'),
            "ads": f"R$ {ads_total:,.2f}".replace(',', 'X').replace('.', ',').replace('X', '.'),
            "unidades": str(int(df['Giro'].sum())),
            "alertas_criticos": int(len(df[df['Sem_Custo'] == True])),
            "periodo_nome": f"Últimos {periodo_dias} dias",
            "imposto_padrao": imposto_padrao_pct
        }

        return jsonify({
            "kpis": kpis, 
            "tabela": df.to_dict(orient='records'), 
            "grafico": grafico_dados, 
            "estoque_parado": estoque_parado,
            "abc": curva_abc,
            "diagnosticos": diagnosticos
        })

    except Exception as e:
        return jsonify({"erro": str(e)}), 500
