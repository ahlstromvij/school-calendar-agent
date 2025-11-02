from flask import Flask
import gmail_process as gmail_process

app = Flask(__name__)

@app.route("/", methods=["GET"])
def home():
    return "Service running ✅", 200

@app.route("/", methods=["POST"])
def run_script():
    gmail_process.main()
    return "Calendar sync complete", 200

def main():
    """Entry point for Poetry or manual invocation."""
    app.run(host="0.0.0.0", port=8080)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
