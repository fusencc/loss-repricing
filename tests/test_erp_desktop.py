"""桌面文件必须不依赖仓库导入，且与项目采用相同批量合同。"""
import importlib.util
from pathlib import Path
import subprocess
import sys
import pytest
from test_erp_bulk_capture import run

ROOT = Path(__file__).resolve().parents[1]


def load_builder():
    path = ROOT / 'scripts/build_erp_desktop.py'
    assert path.exists(), '需要提供桌面独立程序构建器'
    spec = importlib.util.spec_from_file_location('desktop_builder',path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


def load_desktop(tmp_path):
    path = load_builder().build(tmp_path)
    spec = importlib.util.spec_from_file_location('desktop_capture',path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return path,module


@pytest.mark.parametrize('issue',[None,'mismatch','platform','timeout','rate','status','null_status'])
def test_independent_file_runs_same_capture_contract(tmp_path,monkeypatch,issue):
    path,module = load_desktop(tmp_path)
    import test_erp_bulk_capture as contract
    monkeypatch.setattr(contract,'capture_erp_bulk',module.capture_erp_bulk)
    result,calls = run(tmp_path/'data',issue=issue)
    assert result['capture_complete'] is (issue in (None,'status','null_status'))
    assert result['pricing_ready'] is False
    assert 'secret' not in path.read_text().lower().replace('get_secret_value','').replace('secretstr','')


def test_desktop_checkpoint_replaces_previous_json_without_readback(tmp_path, monkeypatch):
    import json

    _, module = load_desktop(tmp_path)
    path = tmp_path / 'output' / 'result.json'
    with monkeypatch.context() as patch:
        patch.setattr(Path, 'read_text', lambda *_a, **_kw: pytest.fail('桌面检查点不应回读'))
        module.write_verified(path, {'步骤': 1})
        module.write_verified(path, {'步骤': 2})
    assert json.loads(path.read_text(encoding='utf-8')) == {'步骤': 2}


def test_standalone_help_and_check_do_not_prompt_or_access_project(tmp_path):
    path,module = load_desktop(tmp_path)
    result=subprocess.run([sys.executable,str(path),'--check'],cwd=tmp_path,capture_output=True,text=True)
    assert result.returncode == 0, result.stderr
    assert '环境检查通过' in result.stdout
    assert not (tmp_path/'output').exists()


def test_desktop_requires_personal_password_and_does_not_persist_it(tmp_path,monkeypatch):
    path,module = load_desktop(tmp_path)
    seen=[]
    monkeypatch.setattr(module,'capture_erp_bulk',lambda settings,**kw: seen.append((settings,kw)) or {
      'summary':{},'groups':[{'error':None}],'sample_only':True,'selection_complete':True,'capture_complete':False,'result_file':'test.json'})
    monkeypatch.setattr(module.getpass,'getpass',lambda prompt:'private-value')
    monkeypatch.setattr('sys.argv',[str(path),'--username','本人','--output',str(tmp_path/'data')])
    assert module.desktop_main()==0
    assert seen[0][0].erp_username=='本人'
    assert seen[0][0].erp_password.get_secret_value()=='private-value'
    assert seen[0][1]['max_orders']==20
    assert all('private-value' not in p.read_text(errors='ignore') for p in tmp_path.rglob('*') if p.is_file() and p.suffix!='.pyc')
