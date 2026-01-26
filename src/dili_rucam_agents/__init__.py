"""dili_rucam_agents package exposing crew builders and pipeline helpers."""


def run_end_to_end(pdf_path: str, prompt_path: str | None = None, output_dir: str | None = None) -> str:
    from .pipeline import run_end_to_end as _run_end_to_end

    return _run_end_to_end(pdf_path, prompt_path, output_dir)


__all__ = ["run_end_to_end"]
