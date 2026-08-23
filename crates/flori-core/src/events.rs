use serde::{Deserialize, Serialize};
use utoipa::ToSchema;

use crate::{
    ArtifactId, ArtifactKind, AttemptId, ErrorCode, JobId, JobState, RunnerId, RunnerState,
    SourceId, SystemHealthStatus, TaskId, TaskLogEvent, TaskState,
};

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize, ToSchema)]
#[serde(deny_unknown_fields)]
pub struct SourceChangedEvent {
    pub source_id: SourceId,
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize, ToSchema)]
#[serde(deny_unknown_fields)]
pub struct JobStateEvent {
    pub job_id: JobId,
    pub state: JobState,
    pub error_code: Option<ErrorCode>,
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize, ToSchema)]
#[serde(deny_unknown_fields)]
pub struct TaskStateEvent {
    pub job_id: JobId,
    pub task_id: TaskId,
    pub state: TaskState,
    pub attempt_id: Option<AttemptId>,
    pub error_code: Option<ErrorCode>,
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize, ToSchema)]
#[serde(deny_unknown_fields)]
pub struct ArtifactCommittedEvent {
    pub job_id: JobId,
    pub task_id: TaskId,
    pub artifact_id: ArtifactId,
    pub kind: ArtifactKind,
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize, ToSchema)]
#[serde(deny_unknown_fields)]
pub struct RunnerChangedEvent {
    pub runner_id: RunnerId,
    pub state: RunnerState,
    pub online: bool,
    pub config_revision: u64,
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize, ToSchema)]
#[serde(deny_unknown_fields)]
pub struct SystemHealthEvent {
    pub status: SystemHealthStatus,
    pub queue_depth: u64,
    pub disk_free_bytes: u64,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize, ToSchema)]
#[serde(
    tag = "kind",
    content = "data",
    rename_all = "snake_case",
    deny_unknown_fields
)]
pub enum JobEventPayload {
    SourceChanged(SourceChangedEvent),
    JobState(JobStateEvent),
    TaskState(TaskStateEvent),
    ArtifactCommitted(ArtifactCommittedEvent),
    LogCursor(TaskLogEvent),
    RunnerChanged(RunnerChangedEvent),
    SystemHealth(SystemHealthEvent),
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize, ToSchema)]
#[serde(deny_unknown_fields)]
pub struct JobEvent {
    pub id: u64,
    pub created_at_ms: u64,
    pub payload: JobEventPayload,
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize, ToSchema)]
#[serde(deny_unknown_fields)]
pub struct SystemView {
    pub status: SystemHealthStatus,
    pub queue_depth: u64,
    pub disk_free_bytes: u64,
    pub runners_total: u64,
    pub runners_online: u64,
    pub usage_started: u64,
    pub usage_final: u64,
}
