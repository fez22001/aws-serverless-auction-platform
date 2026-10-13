"""Bounded test interpreter for the ASL subset actually used by this project.

Expressions execute in JSONata 2.0.6; AWS SDK calls execute against Moto.
Wait delays and Retry backoff are skipped; AWS quotas/IAM/ASL validation are NOT simulated.
A step() API permits deterministic interleavings at real transaction boundaries.
"""
from copy import deepcopy
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from uuid import uuid4
from botocore.exceptions import ClientError

ROOT = Path(__file__).resolve().parents[2]


class Evaluator:
    def __init__(self):
        node = os.environ.get('NODE_BIN') or shutil.which('node') or str(Path.home() / '.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node')
        self.process = subprocess.Popen([node, str(ROOT / 'local/asl/evaluate.cjs')], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)

    def eval(self, value, bindings):
        self.process.stdin.write(json.dumps({'value': value, 'bindings': bindings}) + '\n')
        self.process.stdin.flush()
        line = self.process.stdout.readline()
        assert line, 'JSONata runner exited'
        result = json.loads(line)
        assert 'error' not in result, result
        return result['value']

    def close(self):
        self.process.stdin.close()
        self.process.wait(timeout=5)
        self.process.stdout.close()


class Execution:
    def __init__(self, name, payload, aws, evaluator, hook=None):
        source = (ROOT / f'hw10/step_functions/{name}.json').read_text()
        for key, value in aws['resources'].items():
            source = source.replace('${' + key + '}', value)
        assert '${' not in source
        self.definition = json.loads(source)
        self.current = self.definition['StartAt']
        self.input = deepcopy(payload)
        self.variables = {}
        self.context = {'Execution': {'Id': 'arn:aws:states:us-east-1:123456789012:express:test:' + str(uuid4()), 'Input': payload}}
        self.aws = aws; self.evaluator = evaluator; self.hook = hook
        self.done = False; self.result = None; self.steps = 0
        self.history = []

    def render(self, value, result=None, error=None):
        states = {'input': self.input, 'context': self.context}
        if result is not None:
            states['result'] = result
        if error is not None:
            states['errorOutput'] = error
        return self.evaluator.eval(value, {**self.variables, 'states': states})

    def step(self):
        assert not self.done
        self.steps += 1
        assert self.steps < 300, 'Unbounded workflow loop'
        state = self.definition['States'][self.current]
        self.history.append(self.current)
        kind = state['Type']; result = None
        next_state = state.get('Next')
        if kind == 'Choice':
            next_state = state['Default']
            for rule in state['Choices']:
                if self.render(rule['Condition']):
                    next_state = rule['Next']; break
        elif kind == 'Task':
            args = self.render(state['Arguments'])
            retries = state.get('Retry', [{}])[0].get('MaxAttempts', 0)
            for attempt in range(retries + 1):
                try:
                    if self.hook: self.hook(self, 'before', args)
                    resource = state['Resource']
                    if ':dynamodb:' in resource:
                        method = re.sub(r'(?<!^)(?=[A-Z])', '_', resource.rsplit(':', 1)[1]).lower()
                        result = getattr(self.aws['db'], method)(**args)
                    elif resource.endswith(':sns:publish'):
                        result = self.aws['sns'].publish(**args)
                    else:
                        raise AssertionError('Unsupported Resource: ' + resource)
                    if self.hook: self.hook(self, 'after', args)
                    # datetime metadata cannot enter JSONata; only modeled payload fields do.
                    result.pop('ResponseMetadata', None)
                    break
                except ClientError as exc:
                    if attempt < retries:
                        continue
                    catch = state['Catch'][0]
                    error = {'Error': 'DynamoDb.' + exc.response['Error']['Code'], 'Cause': json.dumps(exc.response)}
                    assignments = self.render(catch.get('Assign', {}), error=error)
                    output = self.render(catch['Output'], error=error) if 'Output' in catch else error
                    self.variables.update(assignments)
                    self.input = output; self.current = catch['Next']
                    return
        elif kind not in ('Pass', 'Wait'):
            raise AssertionError('Unsupported state: ' + kind)
        # Both are evaluated before assignments become visible, matching AWS semantics.
        assignments = self.render(state.get('Assign', {}), result=result)
        output = self.render(state['Output'], result=result) if 'Output' in state else (result if kind == 'Task' else self.input)
        self.variables.update(assignments)
        self.input = output
        if state.get('End'):
            self.done = True; self.result = output
        else:
            self.current = next_state

    def run(self):
        while not self.done: self.step()
        return self.result

    def until(self, state):
        while self.current != state and not self.done: self.step()
        assert self.current == state and not self.done, self.result
        return self
