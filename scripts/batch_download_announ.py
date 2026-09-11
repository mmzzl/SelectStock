import time 
import random
import os
from datetime import datetime
from curl_cffi import requests
from curl_cffi.const import CurlOpt

class StableCurlSession(object):
    def __init__(self):
        self.session = requests.Session()
        self.session.curl_options = {
            CurlOpt.SSL_VERIFYPEER: 0,
            CurlOpt.SSL_VERIFYHOST: 0,
            CurlOpt.TCP_NODELAY: 1,
            CurlOpt.CONNECTTIMEOUT_MS: 15000
        }
        self.impersonate_list = ["chrome131", "chrome124",  "edge101", "firefox133"]

    def get_random_impersonate(self):
        return random.choice(self.impersonate_list)

    def safe_post(self, url ,json=None, params=None, timeout=15):
        time.sleep(random.uniform(0.2, 0.6))
        return self.session.post(url, json=json,timeout=timeout, params=params, 
                                 impersonate=self.get_random_impersonate())

    def safe_get(self, url, params=None, timeout=15):
        time.sleep(random.uniform(0.2, 0.6))
        return self.session.get(
            url,
            params=params,
            timeout=timeout,
            impersonate=self.get_random_impersonate()
        )

    def safe_download_file_stream(self, url, save_path, json=None, params=None, timeout=120):
        time.sleep(random.uniform(0.3, 0.8))
        headers = {"Content-type": "application/pdf"}
        resp = self.session.get(
            url,
            json=json,
            params=params,
            headers=headers,
            timeout=timeout,
            impersonate=self.get_random_impersonate()
        )
        # resp.raise_for_status()
        print(save_path)
        with open(save_path, "wb") as f:
            f.write(resp.content)
        return save_path


stable_req = StableCurlSession()
base_url = "https://www.cninfo.com.cn"
announcement_url = "/new/hisAnnouncement/query"
url = f"{base_url}{announcement_url}"

def get_announcement_list():
    """
    response: 
    {
    "classifiedAnnouncements": null,
    "totalSecurities": 0,
    "totalAnnouncement": 401434,
    "totalRecordNum": 401434,
    "announcements": [
        {
            "id": null,
            "secCode": "601899",
            "secName": "紫金矿业",
            "orgId": "9900004143",
            "announcementId": "1225550225",
            "announcementTitle": "紫金矿业集团股份有限公司关于定期报告财务报表附注部分相关文字错误的更正公告",
            "announcementTime": 1788537600000,
            "adjunctUrl": "finalpage/2026-09-05/1225550225.PDF",
            "adjunctSize": 90,
            "adjunctType": "PDF",
            "storageTime": null,
            "columnId": "250401||251302",
            "pageColumn": "SHZB",
            "announcementType": "01010503||010113||012703",
            "associateAnnouncement": null,
            "important": null,
            "batchNum": null,
            "announcementContent": "",
            "orgName": null,
            "tileSecName": "紫金矿业",
            "shortTitle": "紫金矿业集团股份有限公司关于定期报告财务报表附注部分相关文字错误的更正公告",
            "announcementTypeName": null,
            "secNameList": null
        }]
    "categoryList": null,
    "hasMore": true,
    "totalpages": 13381
    }
    """
    data = {
            "pageNum":	"1",
            "pageSize":	"30",
            "column":	"szse",
            "tabName":	"fulltext",
            "plate":	"",
            "stock":	"",
            "searchkey":	"",
            "secid":	"",
            "category":	"",
            "trade":	"",
            "seDate":	"2026-03-06~2026-09-06",
            "sortName":	"",
            "sortType":	"",
            "isHLtitle"	: True,
        }
    
    res = stable_req.safe_post(url, data)

    print(res.text)

def get_announcement_detail():
    """
    response : 
{
    "classifiedAnnouncements": null,
    "totalSecurities": 0,
    "totalAnnouncement": 2137,
    "totalRecordNum": 2137,
    "announcements": [
        {
            "id": null,
            "secCode": "002281",
            "secName": "光迅科技",
            "orgId": "9900007888",
            "announcementId": "1225550112",
            "announcementTitle": "武汉光迅科技股份有限公司关于拟续聘2026年度审计机构的公告",
            "announcementTime": 1788537600000,
            "adjunctUrl": "finalpage/2026-09-05/1225550112.PDF",
            "adjunctSize": 116,
            "adjunctType": "PDF",
            "storageTime": null,
            "columnId": "09020202||250101||251302",
            "pageColumn": "SZZB",
            "announcementType": "01010503||010112||012301",
            "associateAnnouncement": null,
            "important": null,
            "batchNum": null,
            "announcementContent": "",
            "orgName": null,
            "tileSecName": "光迅科技",
            "shortTitle": "武汉光迅科技股份有限公司关于拟续聘2026年度审计机构的公告",
            "announcementTypeName": null,
            "secNameList": null
        }]
    "categoryList": null,
    "hasMore": true,
    "totalpages": 71
    }
    """
    data = {
            "stock":"002281,9900007888",
            "tabName":"fulltext",
            "pageSize":30,
            "pageNum":1,
            "column":"szse",
            "category":"plate",
            "sz":"seDate",
            "searchkey":"secid",
            "sortName":"sortType",
            "isHLtitle":True
    }
    res = stable_req.safe_post(url, data)
    print(res.text)


def download_announcement(stockCode, announcementId, shortTitle, announcementTime=None):
    """
    下载公告
    response： "announcement":{"id":null,"secCode":"002281","secName":"光迅科技","orgId":"9900007888","announcementId":"1225550112","announcementTitle":"武汉光迅科技股份有限公司关于拟续聘2026年度审计机构的公告","announcementTime":1788537600000,"adjunctUrl":"finalpage/2026-09-05/1225550112.PDF","adjunctSize":116,"adjunctType":"PDF","storageTime":1788527750000,"columnId":"09020202||250101||251302","pageColumn":null,"announcementType":"01010503||010112||012301","associateAnnouncement":null,"important":true,"batchNum":null,"announcementContent":null,"orgName":null,"tileSecName":null,"shortTitle":null,"announcementTypeName":null,"secNameList":null},"fileUrl":"http://static.cninfo.com.cn/finalpage/2026-09-05/1225550112.PDF","bulletinStatus":null,"columIdentify":"qw"}
    """
    api = "/new/announcement/bulletin_detail"
    if not announcementTime:
        announcementTime = datetime.now().strftime("%Y-%m-%d")
    params = {
        "flag": True,
        "announceId": announcementId,
        "announcementTIime": announcementTime
    }
    url = f"{base_url}{api}"
    res = stable_req.safe_post(url, params=params)
    # response
    print(res.text)
    res_data = res.json()
    file_url = res_data.get("fileUrl")
    save_base_path = f"./data/{stockCode}"
    if not os.path.exists(save_base_path):
        os.makedirs(save_base_path)
    save_path = f"{save_base_path}/{shortTitle}.pdf"
    stable_req.safe_download_file_stream(file_url, save_path)


if __name__ == "__main__":
    download_announcement("002281", 1225550112, "紫金矿业集团股份有限公司关于定期报告财务报表附注部分相关文字错误的更正公告", "2026-09-06")
