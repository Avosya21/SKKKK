import pytest

from app.app.services.single_instance import AlreadyRunning, SingleInstance


def test_second_instance_is_refused(tmp_path):
    with SingleInstance("gpu_worker", tmp_path):
        with pytest.raises(AlreadyRunning):
            with SingleInstance("gpu_worker", tmp_path):
                pass


def test_lock_is_released_after_exit(tmp_path):
    with SingleInstance("gpu_worker", tmp_path):
        pass
    with SingleInstance("gpu_worker", tmp_path):
        pass


def test_error_names_the_lock_file(tmp_path):
    with SingleInstance("gpu_worker", tmp_path):
        with pytest.raises(AlreadyRunning) as excinfo:
            with SingleInstance("gpu_worker", tmp_path):
                pass
    assert "gpu_worker.lock" in str(excinfo.value)


def test_different_names_do_not_collide(tmp_path):
    with SingleInstance("gpu_worker", tmp_path):
        with SingleInstance("api", tmp_path):
            pass
