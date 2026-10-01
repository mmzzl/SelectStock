# -*- coding: utf-8 -*-
"""spider 侧运行时配置：复用 stock_selector/config/config.yaml，避免两处库名不一致。"""

import os

import yaml

CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "stock_selector", "config", "config.yaml",
)


class Settings(object):
    def __init__(self, path=CONFIG_PATH):
        with open(path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}

        mongo = cfg.get("mongodb", {})
        self.mongo_host = mongo.get("host", "127.0.0.1")
        self.mongo_port = int(mongo.get("port", 27017))
        self.mongo_uri = os.getenv("SS_MONGO_URI") or "mongodb://%s:%d" % (
            self.mongo_host, self.mongo_port)
        self.mongo_db = os.getenv("SS_MONGO_DB") or mongo.get("database", "eastmoney_news")
        self.mongodb_collection = os.getenv("SS_NEWS_COLLECTION") or mongo.get("collection", "news")
        self.spider_progress_file = (cfg.get("spider", {}) or {}).get(
            "progress_file", "spider_progress.json")


settings = Settings()
