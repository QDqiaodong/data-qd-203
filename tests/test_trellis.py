import itertools,json,unittest
from pathlib import Path
from telemetry_trellis import process
from telemetry_trellis.code import encode

class TrellisTests(unittest.TestCase):
    def setUp(self):self.d=json.loads(Path('fixtures/input.json').read_text())
    def test_encoder_known_vector(self):
        self.assertEqual(encode([1,0,1],3,[7,5])['symbols'],[1,1,1,0,0,0,1,0,1,1])
    def test_one_error_corrected_and_state_trace(self):
        r=process(self.d)['frames'][0];self.assertEqual(r['payload'],[1,0,1]);self.assertEqual(r['corrected_indices'],[0])
        self.assertEqual(r['optimal_paths'],1);self.assertEqual(r['state_path'],[0,1,2,1,2,0])
    def test_all_erased_preserves_ambiguity(self):
        self.d['frames'][0]['received']=[None]*10;r=process(self.d)['frames'][0]
        self.assertEqual(r['optimal_paths'],8);self.assertEqual(r['payload'],[0,0,0]);self.assertEqual(r['distance'],0)
    def test_exhaustive_oracle_for_every_received_word(self):
        self.d.update(constraint=2,generators=[3,1]);f=self.d['frames'][0];f.update(payload_length=2,terminated=False)
        candidates=[(p,encode(p,2,[3,1],False)['symbols']) for p in itertools.product((0,1),repeat=2)]
        for received in itertools.product((0,1,None),repeat=4):
            f['received']=list(received);r=process(self.d)['frames'][0]
            scored=[(sum(a is not None and a!=b for a,b in zip(received,word)),p) for p,word in candidates]
            best=min(c for c,p in scored);winners=sorted(p for c,p in scored if c==best)
            self.assertEqual((r['distance'],r['payload'],r['optimal_paths']),(best,list(winners[0]),len(winners)))
    def test_wrong_length_and_boolean_symbols(self):
        self.d['frames'][0]['received'].pop()
        with self.assertRaises(ValueError):process(self.d)
        self.setUp();self.d['frames'][0]['received'][0]=True
        with self.assertRaises(ValueError):process(self.d)
    def test_unterminated_final_state(self):
        f=self.d['frames'][0];f.update(terminated=False,received=encode([1,1,1],3,[7,5],False)['symbols'])
        r=process(self.d)['frames'][0];self.assertEqual(r['payload'],[1,1,1]);self.assertEqual(r['selected_final_state'],3)
