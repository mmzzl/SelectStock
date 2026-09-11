# -*- coding:utf-8 -*-

class ScheduleTask:
    message = ""
    isok = True
    trigger = None
    invokes = 0

    def __init__(self, script):
        self.script = script

    def error(self, message):
        self.isok = False
        self.message = message