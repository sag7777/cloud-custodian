import fnmatch
import json
import logging
import os
import time

import boto3
import yaml

from janitor.ctx import ExecutionContext
from janitor.credentials import assumed_session
from janitor.manager import resources
from janitor import utils
from janitor.version import version

# This import causes our resources to be initialized
import janitor.resources


def load(options, path, format='yaml'):
    if not os.path.exists(path):
        raise ValueError("Invalid path for config %r" % path)
    
    with open(path) as fh:
        if format == 'yaml':
            data = yaml.load(fh, Loader=yaml.SafeLoader)
        elif format == 'json':
            data = json.load(fh)
    return PolicyCollection(data, options)


class PolicyCollection(object):

    # FIXME: rename data to policySpec?
    def __init__(self, data, options):
        self.data = data
        self.options = options
        
    def policies(self, filters=None):
        # self.options is the CLI options specified in cli.setup_parser()
        policies = [Policy(p, self.options) for p in self.data.get(
            'policies', [])]
        if not filters:
            return policies
        # FIXME: return filter(lambda p: fnmatch.fnmatch(p.name, filters), policies) ?
        sort_order = [p.get('name') for p in self.data.get('policies', [])]
        policy_map = dict([(p.name, p) for p in policies])
        
        matched = fnmatch.filter(policy_map.keys(), filters)
        return [policy_map[n] for n in sort_order if n in matched]

    def __iter__(self):
        return iter(self.policies())
    

class Policy(object):

    log = logging.getLogger('maid.policy')

    def __init__(self, data, options):
        self.data = data
        self.options = options
        assert "name" in self.data
        self.ctx = ExecutionContext(self.session_factory, self, self.options)
        self.resource_manager = self.get_resource_manager()

    @property
    def name(self):
        return self.data['name']

    @property
    def resource_type(self):
        return self.data['resource']

    def process_event(self, event, lambda_ctx, resource):
        """Run policy on a lambda event.

        Lambda automatically generates cloud watch logs, and metrics
        for us.
        """
        results = self.resource_manager.filter_resources([resource])
        if not results:
            return
        for a in self.resource_manager.actions:
            a(results)

    def __call__(self):
        """Run policy in pull mode"""
        with self.ctx:
            self.log.info("Running policy %s" % self.name)
            s = time.time()
            # FIXME: rename resources to distinguish from imported janitor.manager resources
            resources = self.resource_manager.resources()
            rt = time.time() - s
            self.log.info(
                "policy: %s resource:%s has count:%d time:%0.2f" % (
                    self.name, self.resource_type, len(resources), rt))
            self.ctx.metrics.put_metric(
                "ResourceCount", len(resources), "Count", Scope="Policy")
            self.ctx.metrics.put_metric(
                "ResourceTime", rt, "Seconds", Scope="Policy")
            self._write_file('resources.json', utils.dumps(resources, indent=2))

            at = time.time()            
            for a in self.resource_manager.actions:
                s = time.time()
                results = a.process(resources)
                self.log.info(
                    "policy: %s action: %s resources: %d execution_time: %0.2f" % (
                        self.name, a.name, len(resources), time.time()-s))
                self._write_file("action-%s" % a.name, utils.dumps(results))
            self.ctx.metrics.put_metric(
                "ActionTime", time.time() - at, "Seconds", Scope="Policy")
            
    def _write_file(self, rel_p, value):
        with open(
                os.path.join(self.ctx.log_dir, rel_p), 'w') as fh:
            fh.write(value)

    def session_factory(self, assume=True):
        session = boto3.Session(
            region_name=self.options.region,
            profile_name=self.options.profile)
        if self.options.assume_role and assume:
            session = assumed_session(
                self.options.assume_role, "CloudMaid", session)

        # FIXME: split all this maid_record stuff into a function; document it
        maid_record = os.environ.get('MAID_RECORD') # FIXME: what are sane values for this?
        if maid_record:
            maid_record = os.path.expanduser(maid_record)
            if not os.path.exists(maid_record):
                self.log.error(
                    "Maid record path: %s does not exist" % maid_record)
                raise ValueError("record path does not exist")
            self.log.info("Recording aws traffic to: %s" % maid_record)
            import placebo
            pill = placebo.attach(session, maid_record)
            pill.record()

        session._session.user_agent_name = "CloudMaid"
        session._session.user_agent_version = version
        return session
        
    def get_resource_manager(self):
        resource_type = self.data.get('resource')
        factory = resources.get(resource_type)
        if not factory:
            raise ValueError(
                "Invalid resource type: %s" % resource_type)
        return factory(self.ctx, self.data)