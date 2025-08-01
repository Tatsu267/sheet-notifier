import os
import json
from flask import Flask, request, jsonify
from flask_cors import CORS
from sqlalchemy import create_engine, Column, Integer, String, Text
from sqlalchemy.orm import sessionmaker, declarative_base
from pywebpush import webpush, WebPushException

app = Flask(__name__)
CORS(app)

# --- 環境変数から設定を読み込み ---
DATABASE_URL = os.environ.get('DATABASE_URL')
VAPID_PRIVATE_KEY = os.environ.get('VAPID_PRIVATE_KEY')
VAPID_PUBLIC_KEY = os.environ.get('VAPID_PUBLIC_KEY')
VAPID_ADMIN_EMAIL = os.environ.get('VAPID_ADMIN_EMAIL')

# --- データベースの設定 ---
Base = declarative_base()
engine = create_engine(DATABASE_URL)
Session = sessionmaker(bind=engine)

class Subscription(Base):
    __tablename__ = 'subscriptions'
    id = Column(Integer, primary_key=True)
    subscription_json = Column(Text, nullable=False)

Base.metadata.create_all(engine)

# --- APIエンドポイント ---

@app.route('/')
def index():
    return "Notification Server is running."

# 窓口A: 通知の宛先を登録する
@app.route('/subscribe', methods=['POST'])
def subscribe():
    subscription_data = request.get_json()
    if not subscription_data:
        return jsonify({'error': 'No subscription data received'}), 400

    session = Session()
    try:
        new_subscription = Subscription(
            subscription_json=json.dumps(subscription_data)
        )
        session.add(new_subscription)
        session.commit()
        print("新しい通知先を登録しました。")
        return jsonify({'status': 'success'}), 201
    except Exception as e:
        session.rollback()
        print(f"登録エラー: {e}")
        return jsonify({'error': 'Failed to save subscription'}), 500
    finally:
        session.close()

# 窓口B: Tampermonkeyから通知をトリガーする
@app.route('/notify', methods=['POST'])
def notify():
    data = request.get_json()
    count = data.get('employeeCount', 0)

    session = Session()
    try:
        subscriptions = session.query(Subscription).all()
        if not subscriptions:
            print("通知先が登録されていません。")
            return jsonify({'status': 'No subscriptions to notify'}), 200

        notification_payload = json.dumps({
            'title': 'レジカート通知',
            'body': f'待ち状況が {count} 人になりました！',
        })

        for sub in subscriptions:
            try:
                webpush(
                    subscription_info=json.loads(sub.subscription_json),
                    data=notification_payload,
                    vapid_private_key=VAPID_PRIVATE_KEY,
                    vapid_claims={'sub': f"mailto:{VAPID_ADMIN_EMAIL}"}
                )
            except WebPushException as e:
                print(f"通知送信失敗: {e}")
                # 無効な宛先はデータベースから削除する
                if e.response and e.response.status_code == 410:
                    session.delete(sub)
                    session.commit()

        print(f"{len(subscriptions)}件の通知を送信しました。")
        return jsonify({'status': 'Notifications sent'}), 200
    finally:
        session.close()

if __name__ == '__main__':
    app.run(port=int(os.environ.get('PORT', 8080)))
