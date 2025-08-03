import os
import json
from flask import Flask, request, jsonify
from flask_cors import CORS
import gspread
from google.oauth2.service_account import Credentials
from pywebpush import webpush, WebPushException
import traceback
import uuid

app = Flask(__name__)
CORS(app)

# --- 環境変数から設定を読み込み ---
VAPID_PRIVATE_KEY = os.environ.get('VAPID_PRIVATE_KEY')
VAPID_ADMIN_EMAIL = os.environ.get('VAPID_ADMIN_EMAIL')
SPREADSHEET_ID = os.environ.get('SPREADSHEET_ID')
NETLIFY_SITE_URL = os.environ.get('NETLIFY_SITE_URL', 'https://your-site.netlify.app') # デフォルト値を設定

# --- Google Sheetsの設定 ---
SERVICE_ACCOUNT_FILE_PATH = '/etc/secrets/service_account.json'
SHEET_NAME_SUBSCRIPTIONS = '通知先リスト'
SCOPES = [
    'https://www.googleapis.com/auth/spreadsheets',
    'https://www.googleapis.com/auth/drive.file'
]

alert_status = 'inactive'

# (get_spreadsheet_client, get_worksheet, subscribe, respond, send_notification関数は変更ないため省略)
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

@app.route('/')
def index():
    return "Interactive Notification Server is running."

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
            return jsonify({'status': 'updated'}), 200
        else:
            worksheet.append_row([device_name, endpoint, json.dumps(subscription_data)])
            return jsonify({'status': 'success'}), 201
    except Exception:
        return jsonify({'error': 'Failed to save subscription'}), 500

@app.route('/notify', methods=['POST'])
def notify():
    global alert_status
    data = request.get_json()
    count = data.get('employeeCount', 0)

    if alert_status == 'inactive':
        try:
            client = get_spreadsheet_client()
            spreadsheet = client.open_by_key(SPREADSHEET_ID)
            worksheet = get_worksheet(spreadsheet, SHEET_NAME_SUBSCRIPTIONS)
            all_subscriptions = worksheet.get_all_records()
            if not all_subscriptions:
                return jsonify({'status': 'No subscriptions to notify'}), 200

            notification_id = str(uuid.uuid4())
            # ★★★★★ 変更点：通知ペイロードに開くページのURLを追加 ★★★★★
            action_page_url = f"{NETLIFY_SITE_URL}/action.html?nid={notification_id}"
            
            notification_payload = json.dumps({
                'title': 'レジカート応援',
                'body': f'待ち状況が {count} 人です。応援に入ってください！',
                'notificationId': notification_id,
                'actions': [{'action': 'respond', 'title': '応援に入る'}],
                'url': action_page_url # 開くページのURL
            })

            for sub_record in all_subscriptions:
                send_notification(sub_record['SubscriptionJSON'], notification_payload, worksheet)
            
            alert_status = 'pending'
            print(f"応援要請を送信しました。ステータス: {alert_status}")
            return jsonify({'status': 'Notifications sent'}), 200
        except Exception:
            return jsonify({'error': 'Failed to send notifications'}), 500
    else:
        return jsonify({'status': 'skipped'}), 200

@app.route('/reset-alert', methods=['POST'])
def reset_alert():
    global alert_status
    alert_status = 'inactive'
    print(f"通知状態をリセットしました。ステータス: {alert_status}")
    return jsonify({'status': 'alert status reset'}), 200

@app.route('/respond', methods=['POST'])
def respond():
    global alert_status
    data = request.get_json()
    responder_subscription = data.get('subscription')
    if not responder_subscription:
        return jsonify({'error': 'Invalid response data'}), 400
    responder_endpoint = responder_subscription.get('endpoint')
    
    try:
        if alert_status == 'pending':
            alert_status = 'handled'
            client = get_spreadsheet_client()
            spreadsheet = client.open_by_key(SPREADSHEET_ID)
            worksheet = get_worksheet(spreadsheet, SHEET_NAME_SUBSCRIPTIONS)
            all_subscriptions = worksheet.get_all_records()
            responder_name = "不明なデバイス"
            for sub_record in all_subscriptions:
                if sub_record['Endpoint'] == responder_endpoint:
                    responder_name = sub_record['DeviceName']
                    break
            
            response_payload = json.dumps({
                'title': '応援応答',
                'body': f'{responder_name}が応援に入ります。'
            })
            
            for sub_record in all_subscriptions:
                if sub_record['Endpoint'] != responder_endpoint:
                    send_notification(sub_record['SubscriptionJSON'], response_payload, worksheet)
            
            return jsonify({'status': 'Response notification sent'}), 200
        else:
            return jsonify({'status': f'Alert is not pending, but {alert_status}'}), 200
    except Exception:
        return jsonify({'error': 'Failed to process response'}), 500

def send_notification(subscription_json, payload, worksheet):
    try:
        subscription_info = json.loads(subscription_json)
        webpush(
            subscription_info=subscription_info,
            data=payload,
            vapid_private_key=VAPID_PRIVATE_KEY,
            vapid_claims={'sub': f"mailto:{VAPID_ADMIN_EMAIL}"}
        )
    except WebPushException as e:
        if e.response and e.response.status_code == 410:
            endpoint_to_delete = json.loads(subscription_json).get('endpoint')
            try:
                cell = worksheet.find(endpoint_to_delete, in_column=2)
                if cell:
                    worksheet.delete_rows(cell.row)
            except Exception as find_err:
                print(f"無効な宛先の検索/削除中にエラー: {find_err}")

if __name__ == '__main__':
    app.run(port=int(os.environ.get('PORT', 8080)))
