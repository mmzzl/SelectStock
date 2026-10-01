# -*- coding: utf-8 -*-
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import logging
from pymongo import UpdateOne
import requests
import time
import random
from datetime import datetime
import json
from urllib.parse import urlencode
from database import get_db, get_sort_end, query_sort_end
from config.config import settings
from systems.logs import Log
from systems.single import ScriptSingle
from systems.sys import home

class NewsSpider(object):
    BACKFILL_MAX_DAYS = 30

    def __init__(self):
        self.default_params = {
            "client": "web",
            "biz": "web_724",
            "fastColumn": "101",
            "pageSize": 50
        }
        self.base_url = "https://np-weblist.eastmoney.com"
        progress = self.load_progress()
        logging.info("progress: %s", progress)
        self.sort_end = progress.get('sort_end', '')
        self.sort_start = progress.get('sort_start', '')
        self.req_trace = progress.get('req_trace', '')
        self.db = get_db()
        collection = settings.mongodb_collection
        self.collection = self.db[collection]

    def send_request(self, url, method="GET", params=None, data=None):
        """
        发送请求
        """
        try:
            response = requests.request(method, url, params=self.default_params, json=data,  verify=False, timeout=10)
            response.raise_for_status()
            return response.text
        except Exception as e:
            logging.error(f"请求失败: {e}")
            return ""
        
    def _parse_jsonp(self, jsonp_str):
        """解析JSONP格式的响应"""
        # 移除jQuery回调包装
        start = jsonp_str.find('(')
        end = jsonp_str.rfind(')')
        if start != -1 and end != -1:
            json_str = jsonp_str[start+1:end]
            return json.loads(json_str)
        return {}
    
    def _parse_count_response(self, response):
        """解析新闻计数响应"""
        data = self._parse_jsonp(response)
        if data.get('code') == '1':
            return data['data'].get('count', 0)
        return 0

    def get_news_count(self, current_req_trace):
        """
        获取当前是否有新的新闻
        """
        url = f"{self.base_url}/comm/web/getFastNewsCount"
        params = {
            **self.default_params,
            'sortStart': self.sort_start,
            'req_trace': current_req_trace,
            '_': current_req_trace,
            'callback': f'jQuery{random.randint(1000000000000, 9999999999999)}'
        }
        full_url = f"{url}?{urlencode(params)}"
        response = self.send_request(full_url)
        return response

    def get_news(self):
        """
        获取新闻内容
        """
        sort_end = ''
        is_history = False
        current_req_trace = str(int(time.time() *1000))
        news_count = self._parse_count_response(self.get_news_count(current_req_trace))
        logging.info("当前新闻数量: %d", news_count)
        if not news_count:
            # 是否获取历史新闻
            logging.info("没有新的新闻")
            news_item = self.collection.find_one(sort=[("showTime", -1)])
            if news_item:
                show_time = datetime.strptime(news_item['showTime'], '%Y-%m-%d %H:%M:%S')
                now = datetime.now()
                delta = now - show_time
                # 如果差值超过回补上限，不再获取历史数据
                if delta.days > self.BACKFILL_MAX_DAYS:
                    logging.info("新闻数据已超过%d天，停止获取历史数据", self.BACKFILL_MAX_DAYS)
                    # 使用当前时间戳作为sort_end，确保下次只获取最新数据
                    sort_end = str(int(time.time() * 1000))
                    req_trace = int(time.time() * 1000)
                    
                    self.save_progress(sort_end, req_trace)
                    # 直接返回，不再请求新闻列表API
                    return
            logging.info("获取历史新闻")
            is_history = True
            news_response = self.get_history_news(self.sort_end)
            sort_end = self.sort_end
        else:
            # 有新闻，那么sortEnd为空，表示获取最新的新闻列表
            news_url = f'{self.base_url}/comm/web/getFastNewsList'
            news_params = {
                **self.default_params,
                'sortEnd': sort_end,
                'req_trace': current_req_trace,
                '_': current_req_trace,
                'callback': f'jQuery{random.randint(1000000000000, 9999999999999)}'
            }
            full_news_url = f"{news_url}?{urlencode(news_params)}"
            news_response = self.send_request(full_news_url)
        news_list = self.parse_news_response(news_response)
        if not news_list:
            logging.warning("新闻列表为空，跳过本次处理")
            return
        self.sort_start = news_list[0].get('realSort', '')
        self.save_news(news_list)
        if is_history and news_list[-1].get('realSort'):
            # 游标推进到本页最旧一条，下一轮继续往回补而不是重复抓同一页
            sort_end = str(news_list[-1]['realSort'])
            self.sort_end = sort_end
        logging.info(f"sort_end: {sort_end}")
        # 抓最新一轮的 sort_end 为空，不能覆盖掉回补进度
        self.save_progress(sort_end or self.sort_end, current_req_trace)
    
    def _backfill_floor(self):
        """realSort = 毫秒时间戳再补 3 位，据此算出回补的时间下界。"""
        return int((time.time() - self.BACKFILL_MAX_DAYS * 86400) * 1000) * 1000

    def get_history_news(self, sort_end):
        """
        获取历史新闻内容
        """
        # 进度里的游标可能不是有效 realSort（空值或毫秒时间戳），回退到库里最新一条继续往回补
        if not query_sort_end(self.collection, sort_end):
            sort_end = get_sort_end(self.collection)
        if not str(sort_end).isdigit() or int(sort_end) <= self._backfill_floor():
            logging.info("已回补至 %d 天边界，本次跳过历史新闻", self.BACKFILL_MAX_DAYS)
            return ""
        self.sort_end = sort_end
        logging.info(f"历史新闻sortEnd: {sort_end}")
        news_url = f'{self.base_url}/comm/web/getFastNewsList'
    
        news_params = {
            **self.default_params,
            'sortEnd': sort_end,
            'req_trace': sort_end,
            '_': str(int(time.time() *1000)),
            'callback': f'jQuery{random.randint(1000000000000, 9999999999999)}'
        }
        full_news_url = f"{news_url}?{urlencode(news_params)}"
        news_response = self.send_request(full_news_url)
        return news_response


    def save_progress(self, sort_end, req_trace):
        """保存爬取进度"""
        # 这里可以实现将爬取进度保存到数据库的逻辑
        progress_data = {
            'sort_end': sort_end,
            'req_trace': req_trace,
            'sort_start': self.sort_start
        }
        base_path = os.path.dirname(os.path.abspath(__file__))
        progress_path = os.path.join(base_path, settings.spider_progress_file)
        with open(progress_path, 'w', encoding='utf-8') as f:
            json.dump(progress_data, f, ensure_ascii=False, indent=2)
    
    def load_progress(self):
        """加载爬取进度"""
        base_path = os.path.dirname(os.path.abspath(__file__))
        progress_path = os.path.join(base_path, settings.spider_progress_file)
        if os.path.exists(progress_path) and os.path.getsize(progress_path) > 0:
            with open(progress_path, 'r', encoding='utf-8') as f:
                progress_data = json.load(f)
                return progress_data
        else:
            sort_start = str(int(time.time() * 1000))
            return {'sort_end': '', 'req_trace': '', 'sort_start': sort_start}
        
    
    def save_news(self, news_list):
        """保存新闻列表到数据库"""
        # 这里可以实现将新闻列表保存到数据库的逻辑
        collection = settings.mongodb_collection
        if not news_list:
            return 
        operations = []
        for news in news_list:
            filter_query = {'code': news['code']}
            operations.append(
                UpdateOne(filter_query, {'$set': news}, upsert=True)
            )
            if len(operations) >= 100:  # 每100条执行一次批量写入
                self.db[collection].bulk_write(operations)
                operations = []
        if operations:
            self.db[collection].bulk_write(operations)
            logging.info(f"保存了 {len(news_list)} 条新闻到数据库")
    
    def parse_news_response(self, response):
        """解析新闻列表响应"""
        data = self._parse_jsonp(response)
        logging.info("解析新闻响应: %s", response)
        data_list = []
        if data.get('code') == '1':
            if not data.get("data"):
                return []
            news_list = data['data'].get('fastNewsList', [])
            for news in news_list:
                data_list.append(
                    {
                    'code': news.get('code'),
                    'title': news.get('title'),
                    'summary': news.get('summary'),
                    'showTime': news.get('showTime'),
                    'stockList': news.get('stockList', []),
                    'image': news.get('image', []),
                    'pinglun_Num': news.get('pinglun_Num'),
                    'share': news.get('share'),
                    'realSort': news.get('realSort'),
                    'titleColor': news.get('titleColor'),
                    'crawlTime': time.strftime('%Y-%m-%d %H:%M:%S')
                }
                )
                
            return data_list
        return []

if __name__ == "__main__":
    Log("news_spider", log_type=Log.TYPE_FILE, level=logging.INFO)
    pid_file = os.path.join(home(), 'var', 'run', 'news_spider.pid')
    single = ScriptSingle(pid_file)

    if single.is_running():
        logging.error('there is script lock {}'.format(pid_file))
        sys.exit(0)
    spider = NewsSpider()
    news_list = spider.get_news()