import os
import json
from flask import Flask, request, jsonify
from flask_cors import CORS
# (データベースやgspreadなどのインポートは変更なし)
from pywebpush import webpush, WebPushException
import uuid

app = Flask(__name__)
CORS(app)

# (データベースやスプレッドシートへの接続設定は変更なし)

# --- 通知の状態を管理する変数 ---
# 'inactive': 通知可能
# 'pending': 応援要請送信済み、応答待ち
# 'handled': 誰かが応援に入り、リセット待ち
alert_status = 'inactive'

# --- APIエンドポイント ---
@app.route('/')
def index():
    return "Simple Notification Server is running."

# (subscribeエンドポイントは変更なし)
@app.route('/subscribe', methods=['POST'])
def subscribe():
    # ...
    return jsonify({'status': 'success'}), 201

# 窓口A: Tampermonkeyから「応援要請」を受け取る
@app.route('/notify', methods=['POST'])
def notify():
    global alert_status
    data = request.get_json()
    count = data.get('employeeCount', 0)

    # 閾値判断を削除し、状態チェックのみに
    if alert_status == 'inactive':
        # (通知を送信するロジックは変更なし)
        # ...
        alert_status = 'pending' # 状態を「応答待ち」に更新
        print(f"応援要請を送信しました。ステータス: {alert_status}")
        return jsonify({'status': 'Notifications sent'}), 200
    else:
        print(f"通知リクエストを受けましたが、既に通知済みのためスキップしました。")
        return jsonify({'status': 'skipped'}), 200

# 窓口B: Tampermonkeyから「状態リセット」を受け取る
@app.route('/reset-alert', methods=['POST'])
def reset_alert():
    global alert_status
    alert_status = 'inactive'
    print(f"Tampermonkeyからの命令により、通知状態をリセットしました。ステータス: {alert_status}")
    return jsonify({'status': 'alert status reset'}), 200

# (respondエンドポイントは変更なし)
@app.route('/respond', methods=['POST'])
def respond():
    # ...
    return jsonify({'status': 'Response notification sent'}), 200

# (send_notificationヘルパー関数は変更なし)
def send_notification(subscription_json, payload, worksheet):
    # ...

if __name__ == '__main__':
    app.run(port=int(os.environ.get('PORT', 8080)))
