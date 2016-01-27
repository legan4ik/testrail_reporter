from __future__ import absolute_import, print_function

from functools import wraps
import logging

from .testrail import Client as TrClient
from .testrail.client import Run
from .vendor import xunitparser
from .utils import get_testcase_id

logger = logging.getLogger(__name__)


def memoize(f):
    @wraps(f)
    def wrapper(self, *args, **kwargs):
        key = f.__name__
        cached = self._cache.get(key)
        if cached is None:
            cached = self._cache[key] = f(self, *args, **kwargs)
        return cached
    return wrapper


class Reporter(object):

    def __init__(self, xunit_report, iso_link, iso_id, env_description,
                 test_results_link, *args, **kwargs):
        self._config = {}
        self._cache = {}
        self.iso_link = iso_link
        self.iso_id = iso_id
        self.xunit_report = xunit_report
        self.env_description = env_description
        self.test_results_link = test_results_link
        super(Reporter, self).__init__(*args, **kwargs)

    def config_testrail(self, base_url, username, password, milestone, project,
                        tests_suite):
        self._config['testrail'] = dict(
            base_url=base_url,
            username=username,
            password=password,
        )
        self.milestone_name = milestone
        self.project_name = project
        self.tests_suite_name = tests_suite

    @property
    def testrail_client(self):
        return TrClient(**self._config['testrail'])

    @property
    @memoize
    def project(self):
        return self.testrail_client.projects.find(name=self.project_name)

    @property
    @memoize
    def milestone(self):
        return self.project.milestones.find(name=self.milestone_name)

    @property
    @memoize
    def os_config(self):
        return self.project.configs.find(name='Operation System')

    @property
    @memoize
    def suite(self):
        return self.project.suites.find(name=self.tests_suite_name)

    @property
    @memoize
    def cases(self):
        return self.suite.cases()

    @property
    @memoize
    def testrail_statuses(self):
        return self.testrail_client.statuses

    def get_plan_name(self):
        return '{0.milestone_name} iso #{0.iso_id}'.format(self)

    def get_or_create_plan(self):
        """Get exists or create new TestRail Plan"""
        plan_name = self.get_plan_name()
        plan = self.project.plans.find(name=plan_name)
        if plan is None:
            plan = self.project.plans.add(name=plan_name,
                                          description=self.iso_link,
                                          milestone_id=self.milestone.id)
            logger.debug('Created new plan "{}"'.format(plan_name))
        else:
            logger.debug('Founded plan "{}"'.format(plan_name))
        return plan

    def get_xunit_test_suite(self):
        with open(self.xunit_report) as f:
            ts, tr = xunitparser.parse(f)
            return ts, tr

    def add_result_to_case(self, testrail_case, xunit_case):
        if xunit_case.success:
            status_name = 'passed'
        elif xunit_case.failed:
            status_name = 'failed'
        elif xunit_case.skipped:
            # Do not save skipped tests
            return
        elif xunit_case.errored:
            status_name = 'blocked'
        else:
            return
        status_ids = [k for k, v in self.testrail_statuses.items()
                      if v == status_name]
        if len(status_ids) == 0:
            logger.warning("Can't find status {} for result {}".format(
                status_name, xunit_case.methodname))
            return
        status_id = status_ids[0]
        comment = xunit_case.message
        elasped = int(xunit_case.time.total_seconds())
        if elasped > 0:
            elasped = "{}s".format(elasped)
        testrail_case.add_result(
            status_id=status_id,
            elapsed=elasped,
            comment=comment
        )

    def find_testrail_cases(self, xunit_suite):
        cases = self.suite.cases()
        filtered_cases = []
        for xunit_case in xunit_suite:
            test_name = xunit_case.methodname
            case_id = get_testcase_id(test_name)
            if case_id is None:
                logger.warning("Can't extract case ID from {}".format(
                    test_name))
                continue
            testrail_case = cases.find(custom_report_label=case_id)
            if testrail_case is None:
                logger.warning('Testcase for {} not found'.format(test_name))
                continue
            self.add_result_to_case(testrail_case, xunit_case)
            filtered_cases.append(testrail_case)
        cases[:] = filtered_cases
        return cases

    def create_test_run(self, plan, cases):
        suite_name = "{} ({})".format(self.suite.name, self.env_description)
        description = (
            'Run **{suite}** on iso [#{self.iso_id}]({self.iso_link}). \n'
            '[Test results]({self.test_results_link})').format(
                suite=suite_name,
                self=self)
        run = Run(
            name=suite_name,
            description=description,
            suite_id=self.suite.id,
            milestone_id=self.milestone.id,
            config_ids=[],
            case_ids=[x.id for x in cases],
        )
        plan.add_run(run)
        return run

    def print_run_url(self, test_run):
        print('[TestRun URL] {}/index.php?/runs/view/{}'.format(
                self._config['testrail']['base_url'],
                test_run.id))

    def execute(self):
        xunit_suite, _ = self.get_xunit_test_suite()
        cases = self.find_testrail_cases(xunit_suite)
        if len(cases) == 0:
            logger.warning('No cases matched, programm will terminated')
            return
        plan = self.get_or_create_plan()
        test_run = self.create_test_run(plan, cases)
        test_run.add_results_for_cases(cases)
        self.print_run_url(test_run)