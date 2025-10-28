from flask import Flask
import os

app = Flask(__name__)
PORT = int(os.getenv('PORT', 10000))

@app.route('/')
def home():
    return "Quiz Bot is running!"

@app.route('/health')
def health():
    return "OK", 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=PORT, debug=False)
