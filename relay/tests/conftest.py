import os

# gps_relay.main はimport時に必須環境変数を読むため、import前に設定する
os.environ.setdefault("PUSH_AUTH_TOKEN", "test-push-token")
os.environ.setdefault("PULL_AUTH_TOKEN", "test-pull-token")
