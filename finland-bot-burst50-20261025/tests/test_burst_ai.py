import unittest
import hashlib
import sys
from pathlib import Path
from datetime import datetime,timedelta,timezone
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app import burst_ai as b
NOW=datetime(2026,10,25,15,tzinfo=timezone.utc)

def row(title, age=1, source='source1',status='pending'):
    return {'title':title,'text':'Повідомлення про важливі події в Гельсінкі сьогодні.','url':f'https://example.com/{source}/{title.lower().replace(" ","-")}',
            'route':'news','category':'news','status':status,'source_published_at':NOW-timedelta(hours=age)}

class BurstTest(unittest.TestCase):
    def test_no_ingestion_date_freshening(self):
        item=row('Important new health initiative announced');item.pop('source_published_at');item['created_at']=NOW
        self.assertFalse(b.meaningful_news(item,NOW))
    def test_future_date_rejected(self):self.assertFalse(b.meaningful_news(row('A Helsinki community briefing tomorrow',-.5),NOW))
    def test_old_rejected(self):self.assertFalse(b.meaningful_news(row('An older meeting bulletin summary',36),NOW))
    def test_events_not_news(self):
        a=row('Community event announcement in Helsinki');a['route']='events'
        self.assertFalse(b.meaningful_news(a,NOW))
    def test_choose_unique(self):
        a=row('Helsinki launches new public transportation safety campaign')
        c=row('Helsinki launches new public transportation safety campaign',source='elsewhere')
        out=b.choose_candidates([a,c],[],50,NOW)
        self.assertEqual(len(out),1)
    def test_dedupe_tracking_params(self):
        self.assertEqual(b.canonical_url('https://www.example.com/item?utm_source=x&foo=bar'),
                         b.canonical_url('https://example.com/item?foo=bar'))
    def test_priority_sort(self):
        a=row('New city administration financial performance overview',4)
        c=row('Police report major disruption on metro in Helsinki',1)
        self.assertEqual(b.choose_candidates([a,c],[],50,NOW)[0]['title'],c['title'])
    def test_ukrainian_validation(self):
        self.assertTrue(b.looks_ukrainian('У Гельсінкі сьогодні відкривається нова виставка для дітей. Відвідувачі можуть прийти о 12:00.'))
        self.assertFalse(b.looks_ukrainian('Helsingissä järjestetään uusi näyttely lapsille ja perheille. Liput myydään verkossa.'))
    def test_numeric_fidelity(self):
        self.assertFalse(b.faithful_numbers('25.10.2026 18:00', '25.10.2026 19:00'))
        self.assertTrue(b.faithful_numbers('25.10.2026 18:00', '25.10.2026 18:00'))
    def test_limit(self):
        rows=[row('Distinct Finnish bulletin '+' '.join(hashlib.sha256(f'{i}-{j}'.encode()).hexdigest()[0:10] for j in range(7)),source=str(i)) for i in range(75)]
        self.assertEqual(len(b.choose_candidates(rows,[],50,NOW)),50)
    def test_date_event_source(self):
        a=row('Something about past Helsinki event');a['source_published_at']=NOW-timedelta(days=4)
        self.assertFalse(b.meaningful_news(a,NOW))

if __name__=='__main__':unittest.main()
