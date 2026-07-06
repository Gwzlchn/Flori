"""tests for shared/errors.py"""

from shared.errors import (
    RETRY_POLICY,
    AIProviderError,
    AIRateLimitError,
    AITimeoutError,
    AllProvidersFailedError,
    InputInvalidError,
    InputMissingError,
    ProcessingError,
    ResourceError,
    StepError,
    get_retry_delay,
)


class TestErrorTypes:
    def test_base_error_type(self):
        assert StepError.error_type == "unknown"

    def test_input_missing(self):
        e = InputMissingError("file.json")
        assert e.error_type == "input_missing"
        assert "file.json" in str(e)

    def test_input_invalid(self):
        assert InputInvalidError.error_type == "input_invalid"

    def test_processing(self):
        assert ProcessingError.error_type == "processing"

    def test_ai_provider(self):
        assert AIProviderError.error_type == "ai"

    def test_ai_rate_limit(self):
        assert AIRateLimitError.error_type == "ai_rate_limit"

    def test_ai_timeout(self):
        assert AITimeoutError.error_type == "timeout"

    def test_resource(self):
        assert ResourceError.error_type == "resource"

    def test_all_providers_failed(self):
        assert AllProvidersFailedError.error_type == "ai"


class TestInheritance:
    def test_all_inherit_step_error(self):
        for cls in [
            InputMissingError,
            InputInvalidError,
            ProcessingError,
            AIProviderError,
            AIRateLimitError,
            AITimeoutError,
            ResourceError,
            AllProvidersFailedError,
        ]:
            assert issubclass(cls, StepError)

    def test_rate_limit_is_ai_provider(self):
        assert issubclass(AIRateLimitError, AIProviderError)

    def test_all_providers_is_ai_provider(self):
        assert issubclass(AllProvidersFailedError, AIProviderError)

    def test_isinstance_check(self):
        e = AIRateLimitError("rate limited")
        assert isinstance(e, AIProviderError)
        assert isinstance(e, StepError)


class TestRetryPolicy:
    def test_covers_all_error_types(self):
        all_types = {
            cls.error_type
            for cls in [
                InputMissingError,
                InputInvalidError,
                ProcessingError,
                AIProviderError,
                AIRateLimitError,
                AITimeoutError,
                ResourceError,
            ]
        }
        # storage(产物上传失败)由 worker 直接以字符串类型上报,无对应异常类。
        assert all_types | {"storage"} == set(RETRY_POLICY.keys())

    def test_delay_length_ge_max(self):
        for error_type, policy in RETRY_POLICY.items():
            if policy["max"] > 0:
                assert len(policy["delay"]) >= policy["max"], (
                    f"{error_type}: delay length < max"
                )

    def test_no_retry_errors(self):
        assert RETRY_POLICY["input_missing"]["max"] == 0
        assert RETRY_POLICY["input_invalid"]["max"] == 0
        assert RETRY_POLICY["resource"]["max"] == 0


# get_retry_delay 的断言(no-retry/首次/指数退避/超 max/未知)统一收在
# TestBuildVsSystemMatrix,不另设重复用例,避免双份维护面。
class TestBuildVsSystemMatrix:
    """完整重试矩阵:BUILD 为确定性失败不重试,SYSTEM 为瞬态退避重试。"""

    # BUILD 类:首次失败即不重试,get_retry_delay 一律 None。
    def test_build_types_never_retry_at_attempt_zero(self):
        for et in ("input_missing", "input_invalid", "resource"):
            assert get_retry_delay(et, 0) is None, f"{et} 应为 BUILD 不重试"

    def test_unknown_is_build_default_no_retry(self):
        # 步骤内未捕获异常写入 unknown:缺表项按 BUILD 兜底,不重试。
        assert get_retry_delay("unknown", 0) is None

    # SYSTEM 类:每次重试返回配置好的退避秒数。
    def test_ai_exponential_backoff_sequence(self):
        assert [get_retry_delay("ai", a) for a in (0, 1, 2)] == [30, 60, 120]

    def test_ai_rate_limit_increasing_backoff(self):
        # 限流窗口以分钟/小时计:递增长退避耐心等配额恢复,而非短间隔烧完转终态。
        assert [get_retry_delay("ai_rate_limit", a) for a in (0, 1, 2, 3, 4)] == [
            300, 600, 1200, 1800, 1800
        ]

    def test_timeout_single_short_delay(self):
        assert get_retry_delay("timeout", 0) == 10

    def test_processing_immediate_single_retry(self):
        assert get_retry_delay("processing", 0) == 0

    # 超过 max 后所有 SYSTEM 类都停止重试(返回 None)。
    def test_system_types_stop_at_max(self):
        assert get_retry_delay("ai", 3) is None
        assert get_retry_delay("ai_rate_limit", 5) is None
        assert get_retry_delay("timeout", 1) is None
        assert get_retry_delay("processing", 1) is None

    def test_build_types_have_max_zero(self):
        for et in ("input_missing", "input_invalid", "resource"):
            assert RETRY_POLICY[et]["max"] == 0, f"{et} 应为 max 0（BUILD）"

    def test_system_types_have_positive_max(self):
        for et in ("processing", "ai", "ai_rate_limit", "timeout"):
            assert RETRY_POLICY[et]["max"] > 0, f"{et} 应可重试（SYSTEM）"
