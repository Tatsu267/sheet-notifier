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

# --- ヘルパー関数 ---
def get_spreadsheet_client():
    creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE_PATH, scopes=SCOPES)
    return gspread.authorize(creds)

def get_worksheet(spreadsheet, sheet_name):
    try:
        return spreadsheet.worksheet(sheet_name)
    except gspread.WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(title=sheet_name, rows=100, cols=3)
        # ★★★★★ 変更点：ヘッダーにDeviceNameを追加 ★★★★★
        worksheet.append_row(['DeviceName', 'Endpoint', 'SubscriptionJSON'])
        return worksheet

# --- APIエンドポイント ---

@app.route('/')
def index():
    return "Interactive Notification Server (Spreadsheet Backend) is running."

# 窓口A: 通知の宛先と端末名を登録する
@app.route('/subscribe', methods=['POST'])
def subscribe():
    data = request.get_json()
    # ★★★★★ 変更点：deviceNameとsubscriptionの両方を受け取るように変更 ★★★★★
    if not data or 'subscription' not in data or 'deviceName' not in data:
        return jsonify({'error': 'Invalid data format'}), 400

    subscription_data = data.get('subscription')
    device_name = data.get('deviceName')
    endpoint = subscription_data.get('endpoint')

    try:
        client = get_spreadsheet_client()
        spreadsheet = client.open_by_key(SPREADSHEET_ID)
        worksheet = get_worksheet(spreadsheet, SHEET_NAME_SUBSCRIPTIONS)

        # 既に同じendpointが登録されていないかチェック
        cell = worksheet.find(endpoint, in_column=2)
        if cell:
            # 存在する場合は、端末名だけ更新する
            worksheet.update_cell(cell.row, 1, device_name)
            print(f"通知先の端末名を更新しました: {device_name} ({endpoint})")
            return jsonify({'status': 'updated'}), 200

        # 新しい宛先を登録
        worksheet.append_row([device_name, endpoint, json.dumps(subscription_data)])
        print(f"新しい通知先を登録しました: {device_name} ({endpoint})")
        return jsonify({'status': 'success'}), 201
    except Exception as e:
        print(f"登録エラー: {traceback.format_exc()}")
        return jsonify({'error': 'Failed to save subscription'}), 500

# 窓口B: Tampermonkeyから応援要請をトリガーする
@app.route('/notify', methods=['POST'])
def notify():
    data = request.get_json()
    count = data.get('employeeCount', 0)
    notification_id = str(uuid.uuid4())

    try:
        client = get_spreadsheet_client()
        spreadsheet = client.open_by_key(SPREADSHEET_ID)
        worksheet = get_worksheet(spreadsheet, SHEET_NAME_SUBSCRIPTIONS)
        
        all_subscriptions = worksheet.get_all_records() # ヘッダーをキーにした辞書のリストとして取得
        if not all_subscriptions:
            return jsonify({'status': 'No subscriptions to notify'}), 200

        notification_payload = json.dumps({
            'title': 'レジカート応援要請',
            'body': f'待ち状況が {count} 人になりました！',
            'notificationId': notification_id
        })

        for sub_record in all_subscriptions:
            send_notification(sub_record['SubscriptionJSON'], notification_payload, worksheet)

        print(f"{len(all_subscriptions)}件に応援要請を送信しました。")
        return jsonify({'status': 'Notifications sent'}), 200
    except Exception as e:
        print(f"通知エラー: {traceback.format_exc()}")
        return jsonify({'error': 'Failed to send notifications'}), 500

# 窓口C: 「応援に入る」という応答を処理する
@app.route('/respond', methods=['POST'])
def respond():
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

        # 応答した人の端末名を探す
        responder_name = "不明なデバイス"
        for sub_record in all_subscriptions:
            if sub_record['Endpoint'] == responder_endpoint:
                responder_name = sub_record['DeviceName']
                break
        
        print(f"「{responder_name}」から応援の応答がありました。")

        # 応答者以外の全員に通知を送る
        response_payload = json.dumps({
            'title': '応援応答',
            'body': f'「{responder_name}」が応援に入ります。'
        })
        
        for sub_record in all_subscriptions:
            if sub_record['Endpoint'] != responder_endpoint:
                send_notification(sub_record['SubscriptionJSON'], response_payload, worksheet)
        
        return jsonify({'status': 'Response notification sent'}), 200
    except Exception as e:
        print(f"応答処理エラー: {traceback.format_exc()}")
        return jsonify({'error': 'Failed to process response'}), 500


def send_notification(subscription_json, payload, worksheet):
    """プッシュ通知を送信し、失敗した場合はシートから削除するヘルパー関数"""
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
