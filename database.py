# -*- coding: utf-8 -*-
"""同步 Mongo 访问层：供 scripts/ 下的爬虫直接复用。"""

from pymongo import MongoClient

from config.config import settings

_client = None


def get_client():
    global _client
    if _client is None:
        _client = MongoClient(settings.mongo_uri, serverSelectionTimeoutMS=5000)
    return _client


def get_db():
    return get_client()[settings.mongo_db]


def get_sort_end(collection, default=""):
    """取库里最新一条新闻的 realSort，作为续抓历史新闻的游标。"""
    doc = collection.find_one(
        {"realSort": {"$exists": True, "$ne": None}},
        sort=[("realSort", -1)],
        projection={"realSort": 1},
    )
    if not doc or doc.get("realSort") is None:
        return default
    return doc["realSort"]


def query_sort_end(collection, sort_end):
    """进度文件里的 sort_end 是否已经落库（说明库比进度文件更新，需以库为准）。"""
    if not sort_end:
        return False
    return collection.count_documents({"realSort": sort_end}, limit=1) > 0
