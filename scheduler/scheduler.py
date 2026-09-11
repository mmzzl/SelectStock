import sys
import os
import subprocess
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import re
import time
import string
import datetime
from systems.sys import home
from systems.base import ScheduleTask
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_MISSED
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from systems.conf import Conf

pattern = re.compile(r'(?P<num>\d+)(?P<unit>[smhdw])')

class IntervalTask(ScheduleTask):
    def __init__(self, script, interval):
        ScheduleTask.__init__(self, script)

        self.trigger = self.__interval_trigger(interval)

    def __interval_trigger(self, interval):
        result = re.match(pattern, interval)

        if (result):
            unit = result.group("unit")

            try:
                num = int(result.group("num"))
            except Exception as e:
                self.error("interval format is not right : %s" % (interval))
                return None

            if (unit == "s"):
                return IntervalTrigger(seconds=num, jitter=10)

            if (unit == "m"):
                return IntervalTrigger(minutes=num, jitter=10)

            if (unit == "h"):
                return IntervalTrigger(hours=num, jitter=10)

            if (unit == "d"):
                return IntervalTrigger(days=num, jitter=10)

            if (unit == "w"):
                return IntervalTrigger(weeks=num, jitter=10)

            self.error("unkown interval unit : %s" % (unit))
        else:
            self.error("interval format is not right : %s" % (interval))

        return None


class CronTask(ScheduleTask):

    def __init__(self, script, cron):
        ScheduleTask.__init__(self, script)

        self.trigger = self.__cron_trigger(cron)

    # cron format
    # year=,month=,day=,week=,day_of_week=,hour=,miniute=,second=
    def __cron_trigger(self, cron):
        splits = cron.split(",")

        year = None
        month = None
        day = None
        hour = None
        minute = None
        second = None
        week = None
        day_of_week = None

        for arg in splits:
            arg = arg.strip()

            arg_splits = arg.split("=", 1)

            if (len(arg_splits) != 2):
                self.error("cron format is not right : %s" % (cron))
                return None

            name, value = arg_splits

            if name == "year":
                try:
                    year = int(value)
                except Exception as e:
                    self.error("year format is not right : %s" % (value))
                    return None
            elif name == "month":
                try:
                    month = int(value)
                except Exception as e:
                    self.error("month format is not right : %s" % (value))
                    return None
            elif name == "day":
                try:
                    day = int(value)
                except Exception as e:
                    self.error("day format is not right : %s" % (value))
                    return None
            elif name == "hour":
                try:
                    hour = int(value)
                except Exception as e:
                    self.error("hour format is not right : %s" % (value))
                    return None
            elif name == "minute":
                try:
                    minute = int(value)
                except Exception as e:
                    self.error("minute format is not right : %s" % (value))
                    return None
            elif name == "second":
                try:
                    second = int(value)
                except Exception as e:
                    self.error("second format is not right : %s" % (value))
                    return None
            elif name == "week":
                try:
                    week = int(value)
                except Exception as e:
                    self.error("week format is not right : %s" % (value))
                    return None
            elif name == "day_of_week":
                try:
                    day_of_week = int(value)
                except Exception as e:
                    self.error(
                        "day_of_week format is not right : %s" % (value))
                    return None
            else:
                self.error("unkown params : %s=%s" % (name, value))

        return CronTrigger(year=year, month=month, day=day,
                           hour=hour, minute=minute, second=second,
                           week=week, day_of_week=day_of_week)


class DateTask(ScheduleTask):
    def __init__(self, script, date):
        ScheduleTask.__init__(self, script)

        self.trigger = self.__date_trigger(date)

    def __date_trigger(self, date):
        if (date == ""):
            return DateTrigger()

        run_date = None

        try:
            run_date = datetime.datetime.strptime(date, "%Y-%m-%d %H:%M:%S")
        except Exception as e:
            pass

        if run_date is None:
            try:
                run_date = datetime.datetime.strptime(date, "%Y-%m-%d")
            except Exception as e:
                pass

        if run_date is None:
            self.error(
                "date format is not right : %s, only support (2012-12-24) or (2012-12-24 12:12:12)" %
                (date))
            return None

        return DateTrigger(run_date)


def tasks_load_from_inputs(inputs):
    tasks = []
    for section, options in inputs.config.items():
        result = re.match(re.compile(r'(?P<stype>script\d?)://(?P<command>[-"\s\w/.$_]+)'), section)

        if not result:
            continue

        if "enable" in options:
            if options["enable"] == "false":
                continue
        command =   result.group("command") if result else ""
        command = os.path.join(home(), "apps", "api", command)
        script = f"{sys.executable} {command}"
        if "interval" in options:
            task = IntervalTask(script, options["interval"])
        elif "cron" in options:
            task = CronTask(script, options["cron"])
        elif "date" in options:
            task = DateTask(script, options["date"])
        else:
            task = DateTask(script, "")

        tasks.append(task)

    return tasks


def schedule_tasks_load(is_single_this):
    tasks = []

    fantom_home = home()
    input_conf = "inputs.conf"
    inputs = Conf(os.path.join(fantom_home, 'config'), input_conf)

    tasks.extend(tasks_load_from_inputs(inputs=inputs))
    return tasks


class SchedulerManager:
    _instance = None
    _scheduler = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(SchedulerManager, cls).__new__(cls)
        return cls._instance

    def __init__(self):
        if self._scheduler is None:
            self._init_scheduler()

    def _init_scheduler(self):
        job_defaults = {
            'coalesce': True,
            'max_instances': 3,
            'misfire_grace_time': 60
        }

        self._scheduler = BackgroundScheduler(
            job_defaults=job_defaults
        )

        self._scheduler.add_listener(
            self._job_error_listener,
            EVENT_JOB_ERROR
        )
        self._scheduler.add_listener(
            self._job_missed_listener,
            EVENT_JOB_MISSED
        )

    def _job_error_listener(self, event):
        print(f"Job {event.job_id} error: {event.exception}")

    def _job_missed_listener(self, event):
        print(f"Job {event.job_id} missed")

    def add_task(self, task, task_name=None):
        if task.trigger is None:
            print(f"Task {task_name} has no valid trigger, skipped")
            return

        if not task_name:
            task_name = f"task_{task.invokes}"

        self._scheduler.add_job(
            self._execute_task,
            trigger=task.trigger,
            args=[task],
            id=task_name,
            name=task_name,
            replace_existing=True
        )

    def _execute_task(self, task):
        task.invokes += 1
        print(f"Executing task: {task.script}")
        try:
            if task.script:
                result = subprocess.run(task.script, shell=True, capture_output=True, text=True)
                if result.returncode != 0:
                    raise Exception(result.stderr.strip() or f"exit code {result.returncode}")
                if result.stdout.strip():
                    print(f"Task output: {result.stdout.strip()}")
        except Exception as e:
            task.error(str(e))
            print(f"Task execution error: {e}")

    def start(self):
        if not self._scheduler.running:
            self._scheduler.start()
            print("APScheduler started successfully")
        else:
            print("Scheduler is already running")

    def shutdown(self):
        if self._scheduler.running:
            self._scheduler.shutdown()
            print("Scheduler shutdown")

    def get_scheduler(self):
        return self._scheduler


def print_scheduled_jobs(mgr):
    sched = mgr.get_scheduler()
    jobs = sched.get_jobs()
    print(f"Scheduler has {len(jobs)} job(s):")
    for j in jobs:
        print(f"  id={j.id}, trigger={j.trigger}, next_run={j.next_run_time}")


def run_scheduler():
    scheduler_mgr = SchedulerManager()

    tasks = schedule_tasks_load(is_single_this=False)
    print(f"Loaded {len(tasks)} tasks from config:")
    for i, task in enumerate(tasks):
        print(f"  [{i}] script={task.script}, isok={task.isok}, trigger={'yes' if task.trigger else 'NO'}, msg='{task.message}'")
        if task.isok and task.trigger:
            scheduler_mgr.add_task(task, f"task_{i}")
            print(f"    -> Added")
        else:
            print(f"    -> Skipped")

    scheduler_mgr.start()
    print_scheduled_jobs(scheduler_mgr)
    return scheduler_mgr


if __name__ == "__main__":
    
    scheduler = run_scheduler()
    print("调度器已启动，按 Ctrl+C 停止")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n正在关闭调度器...")
        scheduler.shutdown()
        print("调度器已关闭")