#!/usr/bin/env python
# coding=utf-8
import logging
import logging.handlers
# import pylog
# import pystcs
import os
from systems.sys import home
from datetime import datetime

try:
    import syslog
    HAS_SYSLOG = True
except Exception:
    HAS_SYSLOG = False
    syslog = None
    


class Log(object):
    """
    日志管理类, 支持输出日志到控制台
    """

    TYPE_SYS = 'sys'  # 将日志发送到syslog
    TYPE_FILE = 'file'  # 将日志发送到文件
    TYPE_CONSOLE = 'console'  # 将日志发送到控制台
    PYTHON_LOG_CFG = '/home/fantom/apps/secvisual/default/python_log_cfg.ini' # 日志配置文件

    # 日志格式
    FORMAT_DATE = '%Y-%m-%d %H:%M:%S'  # 日期格式
    FORMAT_LOG = '%(asctime)s %(levelname).1s %(process)d %(pathname)s:%(lineno)d (%(funcName)s)\t| %(message)s'

    def __init__(self, log_name, level=logging.INFO, log_type='', log_format='', stcs_conf='', stcs_ns='',
                 exit_hook=True):
        """
        初始化平台日志系统
        :param log_name: 日志存放名称
        :param level: 日志打印级别
        :param log_type: 日志打印到哪里: 控制台(默认) 
        """
        self.format = log_format if log_format else self.FORMAT_LOG
        if self.TYPE_SYS == log_type:
            self._init_sys_log(log_name, level)

        elif self.TYPE_FILE == log_type:
            self._init_file_log(log_name, level)

        else:
            self._init_console(level)

        self._set_disable_log(log_name)
        # 重定向format函数
        logger = logging.getLogger()
       

        
        
    def _init_file_log(self, mode_name, level=logging.INFO):
        """
        将日志写到/var/log/xxx.log，按日期分割
        :param mode_name: 日志文件名
        :param level: 日志级别
        :return:
        """
        filename = f"{home()}/apps/api/logs/sis_%s.log" % mode_name
        log_dir = os.path.dirname(filename)
        if not os.path.exists(log_dir):
            os.makedirs(log_dir, exist_ok=True)

        handler = logging.handlers.TimedRotatingFileHandler(
            filename=filename,
            when='midnight',
            interval=1,
            backupCount=30,
            encoding='utf-8'
        )
        handler.suffix = "%Y-%m-%d"
        handler.setLevel(level)
        handler.setFormatter(logging.Formatter(self.format, self.FORMAT_DATE))

        logger = logging.getLogger()
        logger.setLevel(level)
        logger.addHandler(handler)

    def _init_console(self, level=logging.INFO):
        """
        将日志写到控制台
        :param level: 日志级别
        :return:
        """
        if not logging.getLogger().hasHandlers():
            logging.basicConfig(
                level=level, format=self.format, datefmt=self.FORMAT_DATE)

    def _init_sys_log(self, mode_name, level=logging.INFO):
        """
        将日志写到syslog
        参考: https://www.loggly.com/docs/python-syslog/		https://stackoverflow.com/questions/3968669/how-to-configure-logging-to-syslog-in-python
        :param mode_name:模块名称
        :param level:日志级别
        :return:
        """
        # logging.handlers.SysLogHandler()性能差废弃, 改用syslog库
        # logger = logging.getLogger()
        # logger.setLevel(level)
        # handler = logging.handlers.SysLogHandler('/dev/log')
        # formatter = logging.Formatter(mode_name + ': ' + self._conf['format'], self._conf['datefmt'])
        # handler.setFormatter(formatter)
        # logger.addHandler(handler)

        mode_name = 'sis_' + mode_name  # 平台日志统一加上前缀, 方便分类查看
        logger = logging.getLogger()
        logger.setLevel(level)

        handler = SysLogLibHandler(mode_name)

        formatter = logging.Formatter(self.format, self.FORMAT_DATE)
        handler.setFormatter(formatter)

        logger.handlers = []  # Clearing previous logs
        logger.addHandler(handler)

    def _set_disable_log(self, mode_name):
        """
        判断是否可以输出日志
        :param mode_name: 模块名
        :return:
        """
        pass
        # try:
        #     if not os.path.exists(self.PYTHON_LOG_CFG):
        #         return

        #     conf = configparser.ConfigParser()
        #     conf.read(self.PYTHON_LOG_CFG)
        #     # 允许日志输出
        #     if str(conf.get("setting", "enable")) == "1":
        #         return

        #     # 黑名单过滤
        #     cnt = conf.getint("blacklist", "cnt")
        #     if cnt != 0:
        #         for i in range(0, cnt):
        #             key_str = "value_%s" % str(i)
        #             blacklist = conf.get("blacklist", key_str)
        #             if blacklist in mode_name:
        #                 logging.disable(logging.CRITICAL)
        #                 return

        # except Exception as e:
        #     pass


class SysLogLibHandler(logging.Handler):
    """
    使用syslog库来写入syslog日志, 比logging内置的logging.handlers.SysLogHandler('/dev/log')效率更高
    """

    if HAS_SYSLOG:
        priority_map = {
            logging.DEBUG: syslog.LOG_DEBUG,
            logging.INFO: syslog.LOG_INFO,
            logging.WARN: syslog.LOG_WARNING,
            logging.ERROR: syslog.LOG_ERR,
            logging.CRITICAL: syslog.LOG_CRIT,
            0: syslog.LOG_NOTICE,
        }
    else:
        priority_map = {
            logging.DEBUG: 0,
            logging.INFO: 0,
            logging.WARN: 0,
            logging.ERROR: 0,
            logging.CRITICAL: 0,
            0: 0,
        }

    def __init__(self, model_name):
        if not HAS_SYSLOG:
            raise Exception("Syslog not available on this platform")
        self.pid = os.getpid()
        syslog.openlog(model_name, syslog.LOG_PID)
        logging.Handler.__init__(self)

    def emit(self, record):
        try:
            syslog.syslog(syslog.LOG_USER | self.priority_map.get(
                record.levelno, syslog.LOG_DEBUG), self.format(record))
        except Exception:
            pass
        # syslog.syslog(self.format(record))

    def handle(self, record):
        if os.getpid() != self.pid:  # pid改变，属于fork的子进程，重新创建锁，避免父进程非fork线程正好调用logging造成死锁
            self.createLock()
        return super(SysLogLibHandler, self).handle(record)


class ConsoleLogging:
    """
    打印日志到控制台, 已经废弃, 请使用Log()
    """

    def __init__(self, app, module, level=logging.DEBUG):
        format_message = '[%(levelname)s] [%(asctime)s] [' + app + \
                         ' ' + module + '] [%(filename)s : %(lineno)d] %(message)s'

        logging.basicConfig(level=level, format=format_message)
