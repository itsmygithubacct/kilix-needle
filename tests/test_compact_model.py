import support
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import compact
import compact_model as model


class CandidateTests(unittest.TestCase):
    def test_grammar_fast_path_never_loads_a_candidate(self):
        with patch.object(model,'propose',side_effect=AssertionError('unnecessary load')):
            result=model.decide('start claude here',candidate='unused.json')
        self.assertEqual(result['source'],'grammar')
        self.assertTrue(compact.admit('start claude here',result['calls']).actions)

    def test_model_proposal_still_requires_exact_admission(self):
        proposal=[{'name':'start_claude','arguments':{'directory':'/wrong'}}]
        with patch.object(model,'propose',return_value=(proposal,{})):
            result=model.decide('start claude here',candidate='explicit.json',model_only=True)
        self.assertEqual(result['source'],'model')
        self.assertFalse(compact.admit('start claude here',result['calls']).actions)

    def test_forced_model_cannot_silently_use_grammar(self):
        with self.assertRaises(ValueError):model.decide('start claude',model_only=True)
        with patch.object(model,'propose',side_effect=ValueError('invalid model output')):
            with self.assertRaisesRegex(ValueError,'invalid model'):
                model.decide('start claude',candidate='explicit.json',model_only=True)

    def test_modified_contract_or_library_is_rejected_before_loading(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            member=lambda name,size,sha:{'path':name,'bytes':size,'sha256':sha}
            manifest={'schema':'kilix-compact-candidate-v1','generation':3,
              'library':member('runtime.so',model.LIB_BYTES,'0'*64),
              'weights':member('weights.cact',100,'1'*64),
              'tools':member('tools.json',100,model.TOOLS_SHA256)}
            path=root/'candidate.json';path.write_text(json.dumps(manifest))
            with patch.object(model.asset,'load_verified',side_effect=AssertionError('must not load')):
                with self.assertRaisesRegex(ValueError,'supported contract'):model.propose('start claude',path)

    def test_bundle_members_cannot_escape_the_selected_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for name in ('../elsewhere','/absolute'):
                with self.assertRaises(ValueError):model._member(root,{'path':name,'bytes':1,'sha256':'0'*64},maximum=10)

    def test_cached_candidate_checks_its_cpu_before_loading(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'candidate.json'
            path.write_text(json.dumps({'schema':'kilix-compact-candidate-v1','generation':3,
                'backend':'rust3','cpu_isa':'x86-64-v3'}))
            with patch.object(model,'_supports_x86_v3',return_value=False):
                with self.assertRaisesRegex(ValueError,'x86-64-v3'):model.propose('start claude',path)

    def test_unknown_backend_cannot_select_an_unpinned_library(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'candidate.json'
            path.write_text(json.dumps({'schema':'kilix-compact-candidate-v1','generation':3,
                'backend':'other'}))
            with self.assertRaisesRegex(ValueError,'backend'):model.propose('start claude',path)


if __name__=='__main__':unittest.main()
