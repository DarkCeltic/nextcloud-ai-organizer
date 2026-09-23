"""Focused regression checks for latency, folder semantics and tag fallbacks."""
from __future__ import annotations

from python_organizer_local_llm.suggestion_policy import improve_suggestion
from python_organizer_local_llm.sensitive import safe_suggestion
from python_organizer_local_llm.classifier import Classifier


def sample(**kwargs):
    suggestion = dict(suggested_filename='Resume.pdf', suggested_folder='/Google Takeout/Drive/Jobs',
                      paperless_candidate=False, category='resume', confidence=.98,
                      reason='Model choice', tags=[])
    suggestion.update(kwargs)
    return suggestion


def test_resume_takeout_is_rejected_and_requires_manual_review():
    value = improve_suggestion(sample(), file_path='/AI Inbox/My_Resume.pdf',
                               existing_folders=['/Google Takeout/Drive/Jobs', '/Documents/Resumes'],
                               existing_tags=['Resume', 'takeout'])
    assert value['suggested_folder'] == '/Documents/Resumes'
    assert value['confidence'] < .95
    assert value['tags'] == ['Resume']


def test_resume_folder_proposed_if_none_is_known():
    value = improve_suggestion(sample(), file_path='/AI Inbox/My_Resume.pdf', existing_folders=[])
    assert value['suggested_folder'] == '/Documents/Resumes'


def test_source_inbox_and_foreign_export_rejected():
    note = sample(suggested_filename='Instructions.txt', category='manual',
                  suggested_folder='/AI Inbox', confidence=.99)
    result = improve_suggestion(note, file_path='/AI Inbox/Instructions.txt', existing_tags=['manual'])
    assert result['suggested_folder'] == '/Documents/Unsorted'
    assert result['confidence'] == .69
    assert result['tags'] == ['manual']


def test_existing_tag_exact_match_preferred_over_new_generic_tag():
    note = sample(suggested_folder='/Documents/Manuals', category='manual', tags=[])
    result = improve_suggestion(note, file_path='/AI Inbox/manual.pdf', existing_tags=['Manual', 'Projects'])
    assert result['tags'] == ['Manual']


def test_paperless_candidate_has_complete_nextcloud_alternative():
    note = sample(paperless_candidate=True)
    result = improve_suggestion(note, file_path='/AI Inbox/My_Resume.pdf')
    assert result is note and result['tags'] == ['resume']
    assert result['suggested_folder'] == '/Documents/Resumes'


def test_secret_never_targets_ai_inbox_or_auto_apply():
    note = safe_suggestion('/AI Inbox/cloudflare_token')
    assert note['suggested_folder'] == '/Security/Credentials'
    assert note['confidence'] == 0.0
    assert note['paperless_candidate'] is False


def test_quick_mode_context_is_reset(tmp_path):
    path = tmp_path / 'config.yaml'
    path.write_text('ollama:\n  timeout: 180\n', encoding='utf8')
    classifier = Classifier(str(path))
    assert classifier._quick_mode.get() is False
    with classifier.quick_mode():
        assert classifier._quick_mode.get() is True
    assert classifier._quick_mode.get() is False


def test_new_destination_requires_review_even_with_high_model_confidence():
    item = sample(suggested_folder='/Documents/New and unverified', category='manual',
                  confidence=.99)
    result = improve_suggestion(item, file_path='/AI Inbox/Instructions.txt',
                                existing_folders=['/Documents'])
    assert result['confidence'] < .95
    assert 'not found among existing folders' in result['reason']
