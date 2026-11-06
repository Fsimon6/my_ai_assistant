import pytest

from backend.config.testing import validate_testing_llm


def test_l1_openai_fake_port_is_allowed():
    validate_testing_llm('openai', 'http://127.0.0.1:9110/v1')


def test_local_no_network_probe_port_is_allowed():
    validate_testing_llm('local', 'http://127.0.0.1:1/v1')


@pytest.mark.parametrize('provider,url', [
    ('dashscope', 'https://dashscope.aliyuncs.com/compatible-mode/v1'),
    ('openai', 'https://api.openai.com/v1'),
    ('openai', 'http://127.0.0.1:9100/v1'),
    ('openai', 'http://192.0.2.10:9110/v1'),
    ('local', 'http://127.0.0.1:11434/v1'),
])
def test_real_or_wrong_testing_provider_is_rejected(provider, url):
    with pytest.raises(ValueError):
        validate_testing_llm(provider, url)


@pytest.mark.parametrize('provider,url', [
    ('openai', ''),
    ('openai', 'http://127.0.0.1:9110/v1'),
    ('unknown', 'http://127.0.0.1:9110/v1'),
    ('openai', 'http://user:pass@127.0.0.1:9110/v1'),
])
def test_marker_requires_safe_explicit_configuration(provider, url):
    # The first case must fail; the other cases document accepted/rejected
    # boundaries through the public function rather than by UI inference.
    if provider == 'openai' and url == 'http://127.0.0.1:9110/v1':
        validate_testing_llm(provider, url)
    else:
        with pytest.raises(ValueError):
            validate_testing_llm(provider, url)
