"""Evidence formatting regression cases from saved qualifications."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

FRONT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(FRONT))
from ui.evidence import display_evidence, evidence_text
from ui import api, components


class EvidenceTests(unittest.TestCase):
    def test_multiple_metrics_with_shared_source_keep_all_numbers(self):
        text = evidence_text('Metric path: `metrics.commerce_visibility` — `menu_visible_pct`: 0.0%; `price_visible_pct`: 0.0%; `offer_visible_pct`: 3.85%.')
        self.assertIn('Menu information was visible in 0.0%', text)
        self.assertIn('Price information was visible in 0.0%', text)
        self.assertIn('Offer information was visible in 3.85%', text)
        self.assertNotIn('metrics.', text)
        self.assertEqual(evidence_text('Related content mix: `metrics.content_mix.food_product`: 17 items / 65.38%.'),
                         'Food product: 17 analyzed content items (65.38%).')

    def test_profile_evidence_from_report_is_readable(self):
        original = [
            'profile_analysis.bio.cta_present = false',
            'profile_analysis.contact_accessibility.website_available = false',
            'profile_analysis.contact_accessibility.external_link_available = false',
            'profile_analysis.contact_accessibility.phone_available = false',
            'profile_analysis.contact_accessibility.email_available = true',
            'Profile scraping succeeded: data_quality.profile_scrape_success = true',
        ]
        saved = deepcopy(original)
        shown = display_evidence(original)
        assert len(shown) == 6
        assert 'No call to action was observed in the bio.' in shown
        assert 'The profile analysis recorded an available email contact option.' in shown
        assert 'The Instagram profile was retrieved successfully.' in shown
        assert not any('profile_analysis' in s or '= false' in s for s in shown)
        assert original == saved


    def test_numeric_zero_and_sample_scope_are_preserved(self):
        shown = display_evidence([
            'Research signal: commerce_01',
            'Metric path: metrics.commerce_visibility',
            'metrics.commerce_visibility.menu_visible_pct = 0.0',
            'metrics.commerce_visibility.price_visible_pct = 0.0',
            'Content sample: analysis_coverage.content_analyzed = 4',
            'Content analysis success: analysis_coverage.analysis_success_pct = 100.0',
            'metrics.content_mix.food_product.count = 3',
        ])
        assert len(shown) == 5
        assert 'Menu information was visible in 0.0% of analyzed content.' in shown
        assert 'Content items analyzed: 4.' in shown
        assert 'Content analysis success rate: 100.0%.' in shown
        assert 'Food product: 3 analyzed content items.' in shown


    def test_colon_backticks_and_terminal_punctuation(self):
        assert evidence_text('profile_analysis.bio.cta_present: false') == 'No call to action was observed in the bio.'
        assert evidence_text('`profile_analysis.bio.cta_present` = `false`.') == 'No call to action was observed in the bio.'
        assert evidence_text('metrics.activity.content_per_week = 0.0.') == 'Observed posting frequency: 0.0 content items per week.'
        assert evidence_text('profile_analysis.bio_cta_present = false') == 'No call to action was observed in the bio.'


    def test_unknown_values_are_not_absent_or_zero(self):
        result = evidence_text('profile_analysis.bio.cta_present = null')
        assert 'not available' in result
        assert 'No call to action' not in result
        result = evidence_text('metrics.engagement.engagement_rate_pct = null')
        assert 'not available' in result
        assert '0%' not in result


    def test_existing_sentences_and_numbers_remain(self):
        assert evidence_text('Price visibility: 0%.') == 'Price visibility: 0%.'
        assert evidence_text('Images: 27 of 28 items, or 96.43%') == 'Images: 27 of 28 items, or 96.43%'
        assert evidence_text('Menu information was visible in 5.56%. (Metric Path: metrics.commerce_visibility.menu_visible_pct)') == 'Menu information was visible in 5.56%.'
        assert evidence_text('1 content item in the last 30 days. Metric path: metrics.activity.content_last_30_days.') == '1 content item in the last 30 days.'
        assert evidence_text('`research_signals.commerce_01`') is None
        assert evidence_text('Research signals: commerce_01 and promotion_01.') is None


    def test_home_renders_evidence_without_html_injection(self):
        gap = {'gap': 'Profile actions', 'severity': 'Moderate', 'priority': 1,
               'recommendation_focus': 'Profile', 'description': '', 'rationale': '', 'confidence': 'High',
               'evidence': ['profile_analysis.bio.cta_present = false', '<script>alert(1)</script>']}
        restaurant = {'id': 123, 'name': 'Test Cafe', 'email': 'test@example.test'}
        response = {'counts': {'total': 1, 'high': 0, 'moderate': 1, 'low': 0, 'strengths': 0},
                    'gaps': [gap], 'strengths': [], 'restaurant_name': 'Test Cafe', 'qualification_id': 1}
        with patch.object(components, 'current_restaurant', return_value=restaurant), patch.object(api, 'get_gaps', return_value=response), patch.object(api, 'get_agent_strategy', return_value=None):
            app = AppTest.from_file(str(FRONT / 'app.py'), default_timeout=15)
            app.session_state.authenticated = True
            app.session_state.user_email = restaurant['email']
            app.run()
        assert not app.exception
        html = '\n'.join(m.value for m in app.markdown)
        assert 'No call to action was observed in the bio.' in html
        assert 'profile_analysis.bio.cta_present' not in html
        assert '&lt;script&gt;' in html
        assert '<script>' not in html


if __name__ == "__main__":
    unittest.main()
