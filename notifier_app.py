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

# --- Google Sheetsの設定 ---
SERVICE_ACCOUNT_FILE_PATH = '/etc/secrets/service_account.json'
SHEET_NAME_SUBSCRIPTIONS = '通知先リスト'
SCOPES = [
    'https://www.googleapis.com/auth/spreadsheets',
    'https://www.googleapis.com/auth/drive.file'
]

# --- ★★★★★ 追加：通知の状態を管理する変数 ★★★★★ ---
# 'inactive': 通知不要（初期状態、または状況鎮静後）
# 'pending': 応援要請が送信され、応答を待っている状態
# 'handled': 誰かが応援に入り、台数がリセットされるのを待っている状態
alert_status = 'inactive'
NOTIFICATION_THRESHOLD = 5

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
    return "Interactive Notification Server (Spreadsheet Backend) is running."

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
    except Exception:
        print(f"登録エラー: {traceback.format_exc()}")
        return jsonify({'error': 'Failed to save subscription'}), 500

@app.route('/notify', methods=['POST'])
def notify():
    global alert_status
    data = request.get_json()
    count = data.get('employeeCount', 0)

    try:
        # 状況が落ち着いたら（1人以下）、通知状態をリセット
        if count <= 1:
            if alert_status != 'inactive':
                alert_status = 'inactive'
                print(f"台数が1以下になったため、通知状態をリセットします。ステータス: {alert_status}")
            return jsonify({'status': 'alert reset or not needed'}), 200

        # 閾値を超えていて、かつまだ誰も応援要請を送っていない場合
        if count >= NOTIFICATION_THRESHOLD and alert_status == 'inactive':
            client = get_spreadsheet_client()
            spreadsheet = client.open_by_key(SPREADSHEET_ID)
            worksheet = get_worksheet(spreadsheet, SHEET_NAME_SUBSCRIPTIONS)
            all_subscriptions = worksheet.get_all_records()
            if not all_subscriptions:
                return jsonify({'status': 'No subscriptions to notify'}), 200

            notification_id = str(uuid.uuid4())
            notification_payload = json.dumps({
                'title': 'レジカート応援',
                'body': f'待ち状況が {count} 人です。応援が必要です！',
                'notificationId': notification_id,
                'actions': [{'action': 'respond', 'title': '応援に入る'}] # ボタン付き
            })

            for sub_record in all_subscriptions:
                send_notification(sub_record['SubscriptionJSON'], notification_payload, worksheet)
            
            alert_status = 'pending' # 状態を「応答待ち」に更新
            print(f"応援要請を送信しました。ステータス: {alert_status}")
            return jsonify({'status': 'Notifications sent'}), 200
        
        else:
            print(f"通知は送信されませんでした。現在の台数: {count}, 通知ステータス: {alert_status}")
            return jsonify({'status': 'notification not sent'}), 200

    except Exception:
        print(f"通知エラー: {traceback.format_exc()}")
        return jsonify({'error': 'Failed to send notifications'}), 500

@app.route('/respond', methods=['POST'])
def respond():
    global alert_status
    data = request.get_json()
    responder_subscription = data.get('subscription')
    if not responder_subscription:
        return jsonify({'error': 'Invalid response data'}), 400

    responder_endpoint = responder_subscription.get('endpoint')
    
    try:
        # 応援要請が出ている場合のみ応答を処理
        if alert_status == 'pending':
            alert_status = 'handled' # 状態を「対応中」に更新
            
            client = get_spreadsheet_client()
            spreadsheet = client.open_by_key(SPREADSHEET_ID)
            worksheet = get_worksheet(spreadsheet, SHEET_NAME_SUBSCRIPTIONS)
            all_subscriptions = worksheet.get_all_records()

            responder_name = "不明なデバイス"
            for sub_record in all_subscriptions:
                if sub_record['Endpoint'] == responder_endpoint:
                    responder_name = sub_record['DeviceName']
                    break
            
            print(f"「{responder_name}」からの応答を処理しました。ステータス: {alert_status}")

            response_payload = json.dumps({
                'title': '応援応答',
                'body': f'{responder_name}が応援に入ります。'
                # ボタンは含めない
            })
            
            for sub_record in all_subscriptions:
                if sub_record['Endpoint'] != responder_endpoint:
                    send_notification(sub_record['SubscriptionJSON'], response_payload, worksheet)
            
            return jsonify({'status': 'Response notification sent'}), 200
        else:
            print(f"応答がありましたが、現在のステータスは {alert_status} のため、何もしません。")
            return jsonify({'status': f'Alert is not pending, but {alert_status}'}), 200
    except Exception:
        print(f"応答処理エラー: {traceback.format_exc()}")
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
        print(f"通知送信失敗: {e}")
        if e.response and e.response.status_code == 410:
            endpoint_to_delete = json.loads(subscription_json).get('endpoint')
            cell = worksheet.find(endpoint_to_delete, in_column=2)
            if cell:
                worksheet.delete_rows(cell.row)
                print(f"無効な宛先を削除しました: {endpoint_to_delete}")

if __name__ == '__main__':
    app.run(port=int(os.environ.get('PORT', 8080)))
