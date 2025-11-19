from __future__ import annotations

from langchain_core.runnables import RunnableLambda

from .config import PipelineConfig
from .state import PipelineState
from .stages import abstract_stage, dedupe_stage, notion_stage, summary_stage, watch_stage


def build_pipeline_chain(config: PipelineConfig):
    cfg = config.resolve()
    chain = RunnableLambda(lambda _: PipelineState())
    for stage_fn in (watch_stage, dedupe_stage, summary_stage, abstract_stage, notion_stage):
        chain = chain | RunnableLambda(lambda state, fn=stage_fn: fn(state, cfg))
    return chain


def run_pipeline(config: PipelineConfig) -> PipelineState:
    chain = build_pipeline_chain(config)
    return chain.invoke({})
