import os
import json
from flask import Flask, render_template, request, jsonify, redirect
import firebase_admin
from firebase_admin import credentials, firestore, auth
import requests
import pandas as pd
from datetime import datetime, timedelta
import urllib.parse
import concurrent.futures
import re

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
        if auth.get_user(uid).email != ADMIN_EMAIL: return jsonify({"erro": "Acesso Negado."}), 403
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
    db.collection('custos').document(uid).set({k: float(v) for k, v in custos_novos.items()}, merge=True)
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

# --- 5. ROTA STALKER DE CONCORRENTES ---
@app.route('/api/stalker', methods=['GET', 'POST', 'DELETE'])
def api_stalker():
    uid = verificar_token(request)
    if not uid: return jsonify({"erro": "Acesso Negado"}), 401

    client_uid = request.args.get('client_uid') or (request.json.get('client_uid') if request.is_json else None)
    if client_uid:
        try:
            if auth.get_user(uid).email == ADMIN_EMAIL: uid = client_uid
        except: pass

    doc_ref = db.collection('stalker').document(uid)

    if request.method == 'POST':
        mlb_id = request.json.get('mlb_id', '').strip().upper()
        if not mlb_id: return jsonify({"erro": "ID inválido"}), 400
        # Extrai MLB usando Regex se o usuário colar o link
        match = re.search(r'MLB-?\d+', mlb_id)
        if match: mlb_id = match.group(0).replace('-', '')
        
        dados = doc_ref.get().to_dict() or {'concorrentes': []}
        if mlb_id not in dados['concorrentes']:
            dados['concorrentes'].append(mlb_id)
            doc_ref.set(dados)
        return jsonify({"status": "sucesso"})

    if request.method == 'DELETE':
        mlb_id = request.json.get('mlb_id')
        dados = doc_ref.get().to_dict() or {'concorrentes': []}
        if mlb_id in dados['concorrentes']:
            dados['concorrentes'].remove(mlb_id)
            doc_ref.set(dados)
        return jsonify({"status": "sucesso"})

    # GET: Buscar Preços em Tempo Real
    dados = doc_ref.get().to_dict() or {'concorrentes': []}
    concorrentes_ids = dados.get('concorrentes', [])
    if not concorrentes_ids: return jsonify([])

    ids_str = ",".join(concorrentes_ids)
    res = requests.get(f"https://api.mercadolibre.com/items?ids={ids_str}").json()
    
    resultados = []
    for item in res:
        if item.get('code') == 200:
            body = item['body']
            resultados.append({
                'id': body.get('id'),
                'titulo': body.get('title'),
                'preco': body.get('price'),
                'imagem': body.get('thumbnail'),
                'link': body.get('permalink')
            })
    return jsonify(resultados)

# --- 6. MOTOR FINANCEIRO (DADOS REAIS DA API) ---
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

        # --- BUSCA EXATA DOS FRETES E ESTADOS (VIA MULTITHREADING) ---
        shipping_ids = [str(o.get('shipping', {}).get('id')) for o in resultados if o.get('shipping', {}).get('id')]
        infos_envio = {}
        
        def buscar_info_envio(s_id):
            custo_envio = 0
            estado_destino = "Não Informado"
            headers_frete = {"Authorization": f"Bearer {ml_token}", "x-format-new": "true"}
            
            # Pega Custo
            try:
                res_costs = requests.get(f"https://api.mercadolibre.com/shipments/{s_id}/costs", headers=headers_frete, timeout=5)
                if res_costs.status_code == 200:
                    data = res_costs.json()
                    if 'senders' in data and data['senders']: custo_envio = sum([float(s.get('cost', 0)) for s in data['senders']])
            except: pass
            
            # Pega Estado e Fallback de Custo
            try:
                res_ship = requests.get(f"https://api.mercadolibre.com/shipments/{s_id}", headers=headers_frete, timeout=5)
                if res_ship.status_code == 200:
                    body = res_ship.json()
                    estado_destino = body.get('receiver_address', {}).get('state', {}).get('name', 'Não Informado')
                    if custo_envio == 0:
                        base = float(body.get('base_cost') or 0)
                        buyer = float(body.get('shipping_option', {}).get('cost') or 0)
                        list_cost = float(body.get('shipping_option', {}).get('list_cost') or 0)
                        if buyer == 0: custo_envio = base
                        elif (list_cost > 0 and buyer >= list_cost) or (list_cost == 0 and buyer >= base): custo_envio = 0
                        else: custo_envio = max(0, max(base, list_cost) - buyer)
            except: pass
                
            return s_id, custo_envio, estado_destino

        if shipping_ids:
            shipping_ids = list(set(shipping_ids))
            with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
                futures = [executor.submit(buscar_info_envio, sid) for sid in shipping_ids]
                for future in concurrent.futures.as_completed(futures):
                    sid, custo, estado = future.result()
                    infos_envio[sid] = {'custo': custo, 'estado': estado}

        qty_por_shipment = {}
        for order in resultados:
            ship_id = str(order.get('shipping', {}).get('id', ''))
            if ship_id: qty_por_shipment[ship_id] = qty_por_shipment.get(ship_id, 0) + sum(item['quantity'] for item in order.get('order_items', []))

        agrupado = {}
        timeline = {}
        mapa_calor = {}

        for order in resultados:
            data_venda = order.get('date_created', '')[:10]
            if data_venda not in timeline: timeline[data_venda] = {'faturamento': 0, 'lucro': 0}
            
            ship_id = str(order.get('shipping', {}).get('id', ''))
            info_ship = infos_envio.get(ship_id, {'custo': 0, 'estado': 'Não Informado'})
            frete_total_pedido = info_ship['custo']
            estado_venda = info_ship['estado']
            
            total_qty_pack = qty_por_shipment.get(ship_id, 1)
            custo_envio_unidade = frete_total_pedido / total_qty_pack if total_qty_pack > 0 else 0
            
            faturamento_pedido = 0
            
            for item in order.get('order_items', []):
                item_id = item['item']['id']
                title = item['item']['title']
                qty = item['quantity']
                price = float(item['unit_price'])
                
                custo_cmv = float(custos_db.get(item_id, 0))
                tarifa_ml_unitaria = float(item.get('sale_fee', 0))
                imposto_reais = price * (imposto_padrao_pct / 100)
                lucro_unidade_base = price - custo_cmv - custo_envio_unidade - tarifa_ml_unitaria - imposto_reais
                
                faturamento_pedido += (price * qty)
                timeline[data_venda]['faturamento'] += (price * qty)
                if custo_cmv > 0: timeline[data_venda]['lucro'] += (lucro_unidade_base * qty)

                if item_id not in agrupado:
                    agrupado[item_id] = {
                        'Produto': title, 'Giro': 0, 'Faturamento': 0, 'Ticket_Medio': price,
                        'Custo_CMV': custo_cmv, 'Custo_Frete': 0, 'Custo_Tarifa_ML': 0,
                        'Custo_Imposto': 0, 'Custo_ADS': 0, 'Margem_Contribuicao': 0, 'Sem_Custo': (custo_cmv == 0)
                    }
                
                agrupado[item_id]['Giro'] += qty
                agrupado[item_id]['Faturamento'] += (price * qty)
                agrupado[item_id]['Custo_Frete'] += (custo_envio_unidade * qty)
                agrupado[item_id]['Custo_Tarifa_ML'] += (tarifa_ml_unitaria * qty)
                agrupado[item_id]['Custo_Imposto'] += (imposto_reais * qty)
                agrupado[item_id]['Margem_Contribuicao'] += (lucro_unidade_base * qty) 
            
            if estado_venda != 'Não Informado':
                if estado_venda not in mapa_calor: mapa_calor[estado_venda] = 0
                mapa_calor[estado_venda] += faturamento_pedido

        item_ids_list = list(agrupado.keys())
        if item_ids_list:
            for i in range(0, len(item_ids_list), 50):
                ids_str = ",".join(item_ids_list[i:i+50])
                url_ads = f"https://api.mercadolibre.com/advertising/product_ads/metrics/items?date_from={data_inicio_str}&date_to={data_fim_str}&item_ids={ids_str}"
                try:
                    res_ads = requests.get(url_ads, headers=headers, timeout=10)
                    if res_ads.status_code == 200:
                        for ad_metric in res_ads.json():
                            id_anuncio = ad_metric.get('item_id')
                            if id_anuncio in agrupado: agrupado[id_anuncio]['Custo_ADS'] = float(ad_metric.get('metrics', {}).get('cost', 0))
                except Exception: pass

        dados_reais = []
        for item_id, dados in agrupado.items():
            giro = dados['Giro']
            custo_ads_total = dados['Custo_ADS']
            lucro_total_liquido = dados['Margem_Contribuicao'] - custo_ads_total
            margem_unitaria_final = lucro_total_liquido / giro if giro > 0 else 0

            dados_reais.append({
                'ID': item_id, 'Produto': dados['Produto'], 'Ticket_Medio': dados['Ticket_Medio'],
                'Faturamento': dados['Faturamento'], 'Giro': giro, 'Custo_Frete': dados['Custo_Frete'],
                'Custo_Tarifa_ML': dados['Custo_Tarifa_ML'], 'Custo_Imposto': dados['Custo_Imposto'], 
                'Custo_ADS': custo_ads_total, 'Custo_CMV': dados['Custo_CMV'], 'Sem_Custo': dados['Sem_Custo'],
                'Margem_Contribuicao': margem_unitaria_final, 'Giro_Ant': int(giro * 0.85), 'Margem_Ant': margem_unitaria_final * 0.9 
            })

        df = pd.DataFrame(dados_reais)
        
        if not df.empty:
            df['Giro_Trend'] = df.apply(lambda x: round(((x['Giro'] - x['Giro_Ant']) / x['Giro_Ant']) * 100, 1) if x['Giro_Ant']>0 else 100, axis=1)
            df['MC_Trend'] = df.apply(lambda x: round(((x['Margem_Contribuicao'] - x['Margem_Ant']) / x['Margem_Ant']) * 100, 1) if x['Margem_Ant']!=0 else 0, axis=1)

            def gerar_status(row):
                if row['Sem_Custo']: return "⚠️ Preencha o CMV"
                if row['Margem_Contribuicao'] < 0: return "🔴 Erro de Preço"
                if row['Margem_Contribuicao'] > 0 and row['Custo_ADS'] > 0: return "🟢 Escalar ADS"
                return "🔵 Saudável"
            df['Status'] = df.apply(gerar_status, axis=1)
            df['Desconto_Max'] = (((df['Margem_Contribuicao'] - (df['Ticket_Medio'] * 0.15)) / df['Ticket_Medio']) * 100).round(2)
            df['Desconto_Max_Grafico'] = df['Desconto_Max'].apply(lambda x: max(x, 0))

            lucro_total = float((df[~df['Sem_Custo']]['Giro'] * df[~df['Sem_Custo']]['Margem_Contribuicao']).sum())
            ads_total, faturamento_total = float(df['Custo_ADS'].sum()), float(df['Faturamento'].sum())
        else:
            lucro_total, ads_total, faturamento_total = 0, 0, 0
        
        timeline_ordenada = dict(sorted(timeline.items()))
        grafico_dados = { "labels": list(timeline_ordenada.keys()), "faturamento": [v['faturamento'] for v in timeline_ordenada.values()], "lucro": [v['lucro'] for v in timeline_ordenada.values()] }

        # Processar Mapa de Calor para Frontend
        mapa_lista = sorted([{"estado": k, "faturamento": v} for k, v in mapa_calor.items()], key=lambda x: x['faturamento'], reverse=True)[:6]

        curva_abc = []
        if not df.empty:
            df_abc = df[df['Faturamento'] > 0].sort_values(by='Faturamento', ascending=False)
            faturamento_acumulado = 0
            for _, row in df_abc.iterrows():
                faturamento_acumulado += row['Faturamento']
                pct_acumulada = (faturamento_acumulado / faturamento_total) * 100 if faturamento_total > 0 else 0
                pct_total = (row['Faturamento'] / faturamento_total) * 100 if faturamento_total > 0 else 0
                classe = 'A' if pct_acumulada <= 80 else ('B' if pct_acumulada <= 95 else 'C')
                curva_abc.append({'ID': row['ID'], 'Produto': row['Produto'], 'Faturamento': row['Faturamento'], 'Ticket_Medio': row['Ticket_Medio'], 'Percentual': round(pct_total, 1), 'Classe': classe})

        diagnosticos = []
        if not df.empty:
            for _, row in df.iterrows():
                if row['Sem_Custo'] or row['Giro'] == 0: continue
                margem_pct = (row['Margem_Contribuicao'] / row['Ticket_Medio']) * 100 if row['Ticket_Medio'] > 0 else 0
                if margem_pct < 0: diagnosticos.append({'tipo': 'prejuizo', 'titulo': '🔴 Erro de Precificação', 'produto': row['Produto'], 'mensagem': f"Prejuízo! Margem negativa ({margem_pct:.1f}%). Você perde R$ {abs(row['Margem_Contribuicao']):.2f} por venda."})
                elif 0 <= margem_pct < 15: diagnosticos.append({'tipo': 'alerta_ads' if row['Custo_ADS'] > 0 else 'alerta_margem', 'titulo': '⚠️ Margem Baixa', 'produto': row['Produto'], 'mensagem': f"Sua Margem está no limite ({margem_pct:.1f}%). Cuidado com os custos."})
                elif margem_pct > 30 and row['Giro'] < 5: diagnosticos.append({'tipo': 'escala', 'titulo': '📉 Potencial de Escala', 'produto': row['Produto'], 'mensagem': f"Margem excelente ({margem_pct:.1f}%), mas vendeu pouco ({row['Giro']} unid). Teste baixar o preço."})

        url_itens_ativos = f"https://api.mercadolibre.com/users/{ml_seller_id}/items/search?status=active&limit=50"
        estoque_parado = []
        try:
            res_itens = requests.get(url_itens_ativos, headers=headers).json()
            itens_parados_ids = [i for i in res_itens.get('results', []) if i not in agrupado]
            if itens_parados_ids:
                for i in range(0, len(itens_parados_ids), 20):
                    res_detalhes = requests.get(f"https://api.mercadolibre.com/items?ids={','.join(itens_parados_ids[i:i+20])}", headers=headers).json()
                    for item_obj in res_detalhes:
                        if item_obj.get('code') == 200:
                            body = item_obj['body']
                            estoque_parado.append({'ID': body.get('id'), 'Produto': body.get('title'), 'Preco': body.get('price'), 'Disponivel': body.get('available_quantity', 0), 'Link': body.get('permalink', '#')})
        except: pass

        return jsonify({
            "kpis": { "faturamento": f"R$ {faturamento_total:,.2f}".replace(',','X').replace('.',',').replace('X','.'), "lucro": f"R$ {lucro_total:,.2f}".replace(',','X').replace('.',',').replace('X','.'), "ads": f"R$ {ads_total:,.2f}".replace(',','X').replace('.',',').replace('X','.'), "unidades": str(int(df['Giro'].sum()) if not df.empty else 0), "alertas_criticos": int(len(df[df['Sem_Custo'] == True])) if not df.empty else 0, "periodo_nome": f"Últimos {periodo_dias} dias", "imposto_padrao": imposto_padrao_pct }, 
            "tabela": df.to_dict(orient='records') if not df.empty else [], 
            "grafico": grafico_dados, 
            "mapa_calor": mapa_lista,
            "estoque_parado": estoque_parado,
            "abc": curva_abc,
            "diagnosticos": diagnosticos[:6]
        })

    except Exception as e:
        return jsonify({"erro": str(e)}), 500
