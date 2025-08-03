import os
import json
import uuid
from flask import Flask, request, jsonify
from flask_cors import CORS
from sqlalchemy import create_engine, Column, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import sessionmaker, declarative_base
from pywebpush import webpush, WebPushException
import traceback

app = Flask(__name__)
CORS(app)

# --- 環境変数から設定を読み込み ---
DATABASE_URL = os.environ.get('DATABASE_URL')
VAPID_PRIVATE_KEY = os.environ.get('VAPID_PRIVATE_KEY')
VAPID_ADMIN_EMAIL = os.environ.get('VAPID_ADMIN_EMAIL', 'mailto:admin@example.com')

# --- データベースの設定 ---
Base = declarative_base()
engine = create_engine(DATABASE_URL)
Session = sessionmaker(bind=engine)

class Subscription(Base):
    __tablename__ = 'subscriptions'
    id = Column(Integer, primary_key=True)
    device_name = Column(String, nullable=False) # ★★★★★ 追加：端末名を保存する列 ★★★★★
    endpoint = Column(String, nullable=False, unique=True)
    subscription_json = Column(Text, nullable=False)

Base.metadata.create_all(engine)

# --- APIエンドポイント ---

@app.route('/')
def index():
    return "Interactive Notification Server is running."

# 窓口A: 通知の宛先と端末名を登録する
@app.route('/subscribe', methods=['POST'])
def subscribe():
    data = request.get_json()
    if not data or 'subscription' not in data or 'deviceName' not in data:
        return jsonify({'error': 'Invalid subscription data'}), 400

    subscription_data = data.get('subscription')
    device_name = data.get('deviceName')
    endpoint = subscription_data.get('endpoint')

    session = Session()
    try:
        existing_subscription = session.query(Subscription).filter_by(endpoint=endpoint).first()
        if existing_subscription:
            # 既に存在する場合は、端末名だけ更新する
            existing_subscription.device_name = device_name
            print(f"通知先の端末名を更新しました: {device_name} ({endpoint})")
        else:
            # 新しい宛先を登録
            new_subscription = Subscription(
                device_name=device_name,
                endpoint=endpoint,
                subscription_json=json.dumps(subscription_data)
            )
            session.add(new_subscription)
            print(f"新しい通知先を登録しました: {device_name} ({endpoint})")
        
        session.commit()
        return jsonify({'status': 'success'}), 201
    except Exception as e:
        session.rollback()
        print(f"登録エラー: {traceback.format_exc()}")
        return jsonify({'error': 'Failed to save subscription'}), 500
    finally:
        session.close()

# 窓口B: Tampermonkeyから応援要請をトリガーする
@app.route('/notify', methods=['POST'])
def notify():
    data = request.get_json()
    count = data.get('employeeCount', 0)
    notification_id = str(uuid.uuid4()) # 通知を識別するための一意のIDを生成

    session = Session()
    try:
        subscriptions = session.query(Subscription).all()
        if not subscriptions:
            return jsonify({'status': 'No subscriptions to notify'}), 200

        notification_payload = json.dumps({
            'title': 'レジカート応援要請',
            'body': f'待ち状況が {count} 人になりました！',
            'notificationId': notification_id # ★★★★★ 追加：通知IDをペイロードに含める ★★★★★
        })

        for sub in subscriptions:
            send_notification(sub.subscription_json, notification_payload)

        print(f"{len(subscriptions)}件に応援要請を送信しました。")
        return jsonify({'status': 'Notifications sent'}), 200
    finally:
        session.close()

# ★★★★★ 追加：窓口C: 「応援に入る」という応答を処理する ★★★★★
@app.route('/respond', methods=['POST'])
def respond():
    data = request.get_json()
    responder_subscription = data.get('subscription')
    if not responder_subscription:
        return jsonify({'error': 'Invalid response data'}), 400

    responder_endpoint = responder_subscription.get('endpoint')
    
    session = Session()
    try:
        # 応答した人の端末名を取得
        responder = session.query(Subscription).filter_by(endpoint=responder_endpoint).first()
        if not responder:
            return jsonify({'error': 'Responder not found'}), 404
        
        responder_name = responder.device_name
        print(f"「{responder_name}」から応援の応答がありました。")

        # 応答者以外の全員に通知を送る
        other_subscribers = session.query(Subscription).filter(Subscription.endpoint != responder_endpoint).all()
        
        response_payload = json.dumps({
            'title': '応援応答',
            'body': f'「{responder_name}」が応援に入ります。'
        })

        for sub in other_subscribers:
            send_notification(sub.subscription_json, response_payload)

        print(f"{len(other_subscribers)}件に応答を通知しました。")
        return jsonify({'status': 'Response notification sent'}), 200
    finally:
        session.close()


def send_notification(subscription_json, payload):
    """プッシュ通知を送信するヘルパー関数"""
    try:
        webpush(
            subscription_info=json.loads(subscription_json),
            data=payload,
            vapid_private_key=VAPID_PRIVATE_KEY,
            vapid_claims={'sub': f"mailto:{VAPID_ADMIN_EMAIL}"}
        )
    except WebPushException as e:
        print(f"通知送信失敗: {e}")
        # 無効な宛先はデータベースから削除する
        if e.response and e.response.status_code == 410:
            session = Session()
            endpoint_to_delete = json.loads(subscription_json).get('endpoint')
            stale_sub = session.query(Subscription).filter_by(endpoint=endpoint_to_delete).first()
            if stale_sub:
                session.delete(stale_sub)
                session.commit()
                print(f"無効な宛先を削除しました: {endpoint_to_delete}")
            session.close()

if __name__ == '__main__':
    app.run(port=int(os.environ.get('PORT', 8080)))
