import os
import json
from flask import Flask, request, jsonify
from flask_cors import CORS
import gspread
from google.oauth2.service_account import Credentials
from pywebpush import webpush, WebPushException
import traceback

app = Flask(__name__)
CORS(app)

# --- 環境変数から設定を読み込み ---
VAPID_PRIVATE_KEY = os.environ.get('VAPID_PRIVATE_KEY')
VAPID_ADMIN_EMAIL = os.environ.get('VAPID_ADMIN_EMAIL')
SPREADSHEET_ID = os.environ.get('SPREADSHEET_ID') # スプレッドシートのID

# --- Google Sheetsの設定 ---
SERVICE_ACCOUNT_FILE_PATH = '/etc/secrets/service_account.json' # Secret Fileのパス
SHEET_NAME_SUBSCRIPTIONS = '通知先リスト'
SCOPES = [
    'https://www.googleapis.com/auth/spreadsheets',
    'https://www.googleapis.com/auth/drive.file'
]

# --- ヘルパー関数 ---
def get_spreadsheet_client():
    """スプレッドシートに接続するためのクライアントを取得する"""
    creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE_PATH, scopes=SCOPES)
    return gspread.authorize(creds)

def get_worksheet(spreadsheet, sheet_name):
    """シートを取得し、なければヘッダー付きで作成する"""
    try:
        return spreadsheet.worksheet(sheet_name)
    except gspread.WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(title=sheet_name, rows=100, cols=2)
        worksheet.append_row(['Endpoint', 'SubscriptionJSON'])
        return worksheet

# --- APIエンドポイント ---

@app.route('/')
def index():
    return "Notification Server (Spreadsheet Backend) is running."

# 窓口A: 通知の宛先を登録する
@app.route('/subscribe', methods=['POST'])
def subscribe():
    subscription_data = request.get_json()
    if not subscription_data or 'endpoint' not in subscription_data:
        return jsonify({'error': 'Invalid subscription data'}), 400

    endpoint = subscription_data.get('endpoint')
    try:
        client = get_spreadsheet_client()
        spreadsheet = client.open_by_key(SPREADSHEET_ID)
        worksheet = get_worksheet(spreadsheet, SHEET_NAME_SUBSCRIPTIONS)

        # 既に同じendpointが登録されていないかチェック
        endpoints = worksheet.col_values(1)
        if endpoint in endpoints:
            print(f"通知先は既に登録済みです: {endpoint}")
            return jsonify({'status': 'already registered'}), 200

        # 新しい宛先を登録
        worksheet.append_row([endpoint, json.dumps(subscription_data)])
        print(f"新しい通知先を登録しました: {endpoint}")
        return jsonify({'status': 'success'}), 201
    except Exception as e:
        print(f"登録エラー: {traceback.format_exc()}")
        return jsonify({'error': 'Failed to save subscription'}), 500

# 窓口B: Tampermonkeyから通知をトリガーする
@app.route('/notify', methods=['POST'])
def notify():
    data = request.get_json()
    count = data.get('employeeCount', 0)

    try:
        client = get_spreadsheet_client()
        spreadsheet = client.open_by_key(SPREADSHEET_ID)
        worksheet = get_worksheet(spreadsheet, SHEET_NAME_SUBSCRIPTIONS)
        
        # 2行目以降のB列（SubscriptionJSON）をすべて取得
        subscriptions_str_list = worksheet.col_values(2)[1:]
        if not subscriptions_str_list:
            print("通知先が登録されていません。")
            return jsonify({'status': 'No subscriptions to notify'}), 200

        notification_payload = json.dumps({
            'title': 'レジカート応援要請',
            'body': f'待ち状況が {count} 人になりました！',
        })

        # 削除対象の行番号を記録するリスト
        rows_to_delete = []
        for i, sub_str in enumerate(subscriptions_str_list):
            try:
                subscription_info = json.loads(sub_str)
                webpush(
                    subscription_info=subscription_info,
                    data=notification_payload,
                    vapid_private_key=VAPID_PRIVATE_KEY,
                    vapid_claims={'sub': f"mailto:{VAPID_ADMIN_EMAIL}"}
                )
            except WebPushException as e:
                print(f"通知送信失敗: {e}")
                # 無効な宛先は削除対象として記録
                if e.response and e.response.status_code == 410:
                    # i+2 で正しい行番号を指定 (1始まりのインデックス + ヘッダー行)
                    rows_to_delete.append(i + 2)
            except Exception as e:
                print(f"通知処理中の予期せぬエラー: {e}")
        
        # 無効な宛先をまとめて削除（逆順にソートして、下の行から削除）
        if rows_to_delete:
            for row_num in sorted(rows_to_delete, reverse=True):
                worksheet.delete_rows(row_num)
                print(f"無効な宛先を行 {row_num} から削除しました。")

        print(f"{len(subscriptions_str_list)}件の通知を送信しました。")
        return jsonify({'status': 'Notifications sent'}), 200
    except Exception as e:
        print(f"通知エラー: {traceback.format_exc()}")
        return jsonify({'error': 'Failed to send notifications'}), 500

if __name__ == '__main__':
    app.run(port=int(os.environ.get('PORT', 8080)))
