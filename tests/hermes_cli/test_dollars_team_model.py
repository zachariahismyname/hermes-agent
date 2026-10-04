import contextlib, importlib.util, io, json, pathlib, tempfile, unittest
from unittest.mock import patch
from urllib.error import URLError, HTTPError
from ruamel.yaml import YAML

spec = importlib.util.spec_from_file_location('model_script', pathlib.Path(__file__).resolve().parents[2] / 'maintenance/dollars/team-model/set_hermes_team_model.py')
module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)

class OfflineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp.name)
        self.files = [self.root/'config.yaml', self.root/'profiles'/'dollars'/'config.yaml']
        self.sample = {'model': {'default':'gpt-6-sol','provider':'openai-codex'},
            'mcp_servers':{'hindsight':{'url':'http://localhost/mcp/private/'}}, 'terminal':{'cwd':'D:\\Dollars'}}
        for file in self.files:
            file.parent.mkdir(parents=True,exist_ok=True)
            file.write_text(json.dumps(self.sample),encoding='utf-8')
        self.args=['script','--model','gpt-6.1-sol','--thinking','medium','--speed','normal']
    def tearDown(self): self.temp.cleanup()
    def run_script(self, api, extra):
        output=io.StringIO()
        with patch.object(module,'ROOT',self.root), patch.object(module,'api',side_effect=api) as calls, patch('sys.argv',self.args+extra), contextlib.redirect_stdout(output):
            module.main()
        return output.getvalue(),calls
    def offline(self,*args,**kwargs): raise URLError(ConnectionRefusedError('offline'))
    def verify_saved(self):
        for path in self.files:
            data=YAML(typ='safe').load(path.read_text())
            self.assertEqual(data['model']['default'],'gpt-6.1-sol')
            self.assertEqual(data['agent']['reasoning_effort'],'medium')
            self.assertEqual(data['mcp_servers'],self.sample['mcp_servers'])
            self.assertTrue(list(path.parent.glob('config.yaml.before-team-model-*')))
    def test_offline_apply(self):
        text,_=self.run_script(self.offline,['--apply']); self.verify_saved()
        self.assertIn('synchronization skipped',text)
        self.assertIn('synchronized 0 Paperclip agents',text)
    def test_offline_preview(self):
        text,_=self.run_script(self.offline,[])
        for path in self.files: self.assertEqual(json.loads(path.read_text()),self.sample)
        self.assertFalse(list(self.root.rglob('*.before-team-model-*')))
    def test_explicit_hermes_only(self):
        _,calls=self.run_script(self.offline,['--apply','--hermes-only'])
        calls.assert_not_called(); self.verify_saved()
    def test_auth_error_not_disguised_as_offline(self):
        def denied(*args): raise HTTPError('http://localhost',401,'Unauthorized',{},None)
        with self.assertRaises(HTTPError): self.run_script(denied,['--apply'])
        for path in self.files: self.assertEqual(json.loads(path.read_text()),self.sample)
    def test_disconnect_after_local_apply(self):
        def fake(base,method,route,body=None):
            if route=='/companies': return [{'id':'company'}]
            if route=='/companies/company/agents': return [{'id':'a','name':'dollars','adapterType':'hermes_local','adapterConfig':{'model':'gpt-6-sol','provider':'openai-codex','extraArgs':['--profile','dollars']}}]
            raise URLError('disconnected')
        text,_=self.run_script(fake,['--apply']); self.verify_saved()
        self.assertIn('connection lost',text)
    def test_online_sync(self):
        def fake(base,method,route,body=None):
            cfg={'model':'gpt-6-sol','provider':'openai-codex','extraArgs':['--profile','dollars']}
            if route=='/companies': return [{'id':'company'}]
            if route=='/companies/company/agents': return [{'id':'a','name':'dollars','adapterType':'hermes_local','adapterConfig':cfg}]
            return {'adapterConfig':body['adapterConfig'] if method=='PATCH' else cfg}
        text,calls=self.run_script(fake,['--apply']); self.verify_saved()
        self.assertIn('synchronized 1 Paperclip agents',text)
        self.assertEqual(sum(c.args[1]=='PATCH' for c in calls.call_args_list),1)

if __name__ == '__main__':
    unittest.main()
