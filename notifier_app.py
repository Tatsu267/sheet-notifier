import os
import json
from flask import Flask, request, jsonify
from flask_cors import CORS
import gspread
from google.oauth2.service_account import Credentials
from pywebpush import webpush, WebPushException
import traceback
import uuid
from threading import Lock
import time

app = Flask(__name__)
CORS(app)

# --- 環境変数から設定を読み込み ---
VAPID_PRIVATE_KEY = os.environ.get('VAPID_PRIVATE_KEY')
VAPID_ADMIN_EMAIL = os.environ.get('VAPID_ADMIN_EMAIL')
SPREADSHEET_ID = os.environ.get('SPREADSHEET_ID')
NETLIFY_SITE_URL = os.environ.get('NETLIFY_SITE_URL', 'https://your-site.netlify.app')

# --- Google Sheetsの設定 ---
SERVICE_ACCOUNT_FILE_PATH = '/etc/secrets/service_account.json'
SHEET_NAME_SUBSCRIPTIONS = '通知先リスト'
SCOPES = [
    'https://www.googleapis.com/auth/spreadsheets',
    'https://www.googleapis.com/auth/drive.file'
]

# --- 通知の状態管理 ---
alert_state = {'state': 'inactive', 'responder_name': None, 'last_notify_time': 0}
state_lock = Lock()
NOTIFICATION_COOLDOWN = 60 # 60秒のクールダウン

# --- ヘルパー関数 ---
def get_spreadsheet_client():
    creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE_PATH, scopes=SCOPES)
    return gspread.authorize(creds)

def get_worksheet(spreadsheet, sheet_name):
    try:
        return spreadsheet.worksheet(sheet_name)
    except gspread.WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(title=sheet_name, rows=100, cols=3)
        worksheet.append_row(['DeviceName', 'Endpoint', 'SubscriptionJSON'])
        return worksheet

# --- APIエンドポイント ---

@app.route('/')
def index():
    return "Advanced Interactive Notification Server is running."

@app.route('/subscribe', methods=['POST'])
def subscribe():
    data = request.get_json()
    if not data or 'subscription' not in data or 'deviceName' not in data:
        return jsonify({'error': 'Invalid data format'}), 400

    subscription_data = data.get('subscription')
    device_name = data.get('deviceName')
    endpoint = subscription_data.get('endpoint')

    try:
        client = get_spreadsheet_client()
        spreadsheet = client.open_by_key(SPREADSHEET_ID)
        worksheet = get_worksheet(spreadsheet, SHEET_NAME_SUBSCRIPTIONS)
        cell = worksheet.find(endpoint, in_column=2)
        if cell:
            worksheet.update_cell(cell.row, 1, device_name)
            print(f"通知先の端末名を更新しました: {device_name}")
            return jsonify({'status': 'updated'}), 200
        else:
            worksheet.append_row([device_name, endpoint, json.dumps(subscription_data)])
            print(f"新しい通知先を登録しました: {device_name}")
            return jsonify({'status': 'success'}), 201
    except Exception as e:
        print(f"登録エラー: {traceback.format_exc()}")
        return jsonify({'error': 'Failed to save subscription'}), 500

@app.route('/notify', methods=['POST'])
def notify():
    global alert_state
    data = request.get_json()
    count = data.get('employeeCount', 0)
    now = time.time()

    with state_lock:
        if now - alert_state.get('last_notify_time', 0) > NOTIFICATION_COOLDOWN:
            if alert_state['state'] == 'handled':
                print(f"通知リクエストを受けましたが、既に「{alert_state['responder_name']}」が対応中のため、再通知はスキップします。")
                return jsonify({'status': 'skipped, already handled'}), 200

            try:
                client = get_spreadsheet_client()
                spreadsheet = client.open_by_key(SPREADSHEET_ID)
                worksheet = get_worksheet(spreadsheet, SHEET_NAME_SUBSCRIPTIONS)
                all_subscriptions = worksheet.get_all_records()
                if not all_subscriptions:
                    return jsonify({'status': 'No subscriptions to notify'}), 200

                notification_id = str(uuid.uuid4())
                action_page_url = f"{NETLIFY_SITE_URL}/action.html?nid={notification_id}"
                
                notification_payload = json.dumps({
                    'title': 'レジカート応援',
                    'body': f'待ち状況が {count} 人です。応援に入ってください！',
                    'notificationId': notification_id,
                    'actions': [{'action': 'respond', 'title': '応援に入る'}],
                    'url': action_page_url
                })

                for sub_record in all_subscriptions:
                    send_notification(sub_record['SubscriptionJSON'], notification_payload, worksheet)
                
                alert_state['state'] = 'pending'
                alert_state['last_notify_time'] = now
                print(f"応援要請を送信しました。ステータス: {alert_state['state']}")
                return jsonify({'status': 'Notifications sent'}), 200
            except Exception as e:
                print(f"通知送信エラー: {traceback.format_exc()}")
                return jsonify({'error': 'Failed to send notifications'}), 500
        
        return jsonify({'status': 'notification skipped due to cooldown'}), 200

@app.route('/reset-alert', methods=['POST'])
def reset_alert():
    global alert_state
    with state_lock:
        if alert_state['state'] != 'inactive':
            print(f"Tampermonkeyからの命令により、通知状態をリセットします。")
            alert_state = {'state': 'inactive', 'responder_name': None, 'last_notify_time': 0}
    return jsonify({'status': 'alert status reset'}), 200

@app.route('/respond', methods=['POST'])
def respond():
    global alert_state
    data = request.get_json()
    responder_subscription = data.get('subscription')
    if not responder_subscription:
        return jsonify({'error': 'Invalid response data'}), 400

    responder_endpoint = responder_subscription.get('endpoint')
    
    try:
        client = get_spreadsheet_client()
        spreadsheet = client.open_by_key(SPREADSHEET_ID)
        worksheet = get_worksheet(spreadsheet, SHEET_NAME_SUBSCRIPTIONS)
        all_subscriptions = worksheet.get_all_records()

        responder_name = "不明なデバイス"
        for sub_record in all_subscriptions:
            if sub_record['Endpoint'] == responder_endpoint:
                responder_name = sub_record['DeviceName']
                break

        with state_lock:
            if alert_state['state'] == 'inactive':
                payload = json.dumps({'title': '応援不要', 'body': 'この応援要請は既に解決されています。'})
                send_notification(json.dumps(responder_subscription), payload, worksheet)
                return jsonify({'status': 'Alert was already inactive'}), 200
            elif alert_state['state'] == 'pending':
                alert_state = {'state': 'handled', 'responder_name': responder_name, 'last_notify_time': alert_state['last_notify_time']}
                print(f"「{responder_name}」が最初の応答者です。ステータス: {alert_state}")
                
                payload = json.dumps({'title': '応援応答', 'body': f'「{responder_name}」が応援に入ります。'})
                for sub_record in all_subscriptions:
                    if sub_record['Endpoint'] != responder_endpoint:
                        send_notification(sub_record['SubscriptionJSON'], payload, worksheet)
                return jsonify({'status': 'Response accepted'}), 200
            elif alert_state['state'] == 'handled':
                original_responder = alert_state['responder_name']
                print(f"「{responder_name}」から応答がありましたが、既に「{original_responder}」が対応中です。")
                
                payload = json.dumps({'title': '応援重複', 'body': f'ありがとうございます。既に「{original_responder}」が応援に入っています。'})
                send_notification(json.dumps(responder_subscription), payload, worksheet)
                return jsonify({'status': 'Alert was already handled'}), 200
        
        return jsonify({'status': 'ok'}), 200

    except Exception as e:
        print(f"応答処理エラー: {traceback.format_exc()}")
        return jsonify({'error': 'Failed to process response'}), 500

def send_notification(subscription_json, payload, worksheet):
    try:
        subscription_info = json.loads(subscription_json)
        webpush(
            subscription_info=subscription_info,
            data=payload,
            vapid_private_key=VAPID_PRIVATE_KEY,
            vapid_claims={'sub': f"mailto:{VAPID_ADMIN_EMAIL}"},
            # ★★★★★ 追加：通知の優先度を最高に設定 ★★★★★
            ttl=3600, # 1時間以内に届かなければ諦める
            headers={'Urgency': 'high'} # 緊急度を「高」に設定
        )
    except WebPushException as e:
        print(f"通知送信失敗: {e}")
        if e.response and e.response.status_code == 410:
            endpoint_to_delete = json.loads(subscription_json).get('endpoint')
            try:
                cell = worksheet.find(endpoint_to_delete, in_column=2)
                if cell:
                    worksheet.delete_rows(cell.row)
                    print(f"無効な宛先を削除しました: {endpoint_to_delete}")
            except Exception as find_err:
                print(f"無効な宛先の検索/削除中にエラー: {find_err}")

if __name__ == '__main__':
    app.run(port=int(os.environ.get('PORT', 8080)))
