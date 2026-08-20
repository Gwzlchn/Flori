use super::*;
use flori_core::{CompiledTaskSpec, TaskInputBindings};

impl CompiledTask {
    pub fn freeze_for_job(&self) -> Result<(CompiledTaskSpec, TaskInputBindings), CompileError> {
        let reference = |key: &str| match self.inputs.get(key) {
            Some(InputValue::Reference(value)) => Ok(value.clone()),
            _ => Err(invalid()),
        };
        let optional = |key: &str| match self.inputs.get(key) {
            Some(InputValue::Reference(value)) => Ok(Some(value.clone())),
            None => Ok(None),
            _ => Err(invalid()),
        };
        let bindings = match self.executor {
            Executor::DocumentAcquire => TaskInputBindings::DocumentAcquire {
                source: reference("source")?,
            },
            Executor::DocumentExtract => TaskInputBindings::DocumentExtract {
                pdf: reference("pdf")?,
            },
            Executor::AiDocumentTranslate => TaskInputBindings::AiDocumentTranslate {
                document: reference("document")?,
                prompt: reference("prompt")?,
                profile: optional("profile")?,
            },
            Executor::AiDocumentNote => TaskInputBindings::AiDocumentNote {
                document: reference("document")?,
                prompt: reference("prompt")?,
                profile: optional("profile")?,
            },
            Executor::VideoAcquire => TaskInputBindings::VideoAcquire {
                source: reference("source")?,
            },
            Executor::VideoSubscription => TaskInputBindings::VideoSubscription {
                source: reference("source")?,
            },
            Executor::VideoTranscribe => TaskInputBindings::VideoTranscribe {
                video: reference("video")?,
                subtitle: optional("subtitle")?,
            },
            Executor::VideoFrames => TaskInputBindings::VideoFrames {
                video: reference("video")?,
                transcript: reference("transcript")?,
            },
            Executor::VideoMechanicalNote => TaskInputBindings::VideoMechanicalNote {
                transcript: reference("transcript")?,
                frames: reference("frames")?,
            },
            Executor::AiVideoNote => TaskInputBindings::AiVideoNote {
                transcript: reference("transcript")?,
                mechanical_note: reference("mechanical_note")?,
                frames: reference("frames")?,
                prompt: reference("prompt")?,
                profile: optional("profile")?,
            },
            Executor::CoreValidate => TaskInputBindings::CoreValidate {
                source: reference("source")?,
                notes: reference("notes")?,
            },
            Executor::CorePublish => TaskInputBindings::CorePublish {
                validated: reference("validated")?,
            },
        };
        if !bindings.is_valid() {
            return Err(invalid());
        }
        Ok((
            CompiledTaskSpec {
                executor: self.executor,
                needs: self.needs.clone(),
                tags: self.tags.clone(),
                retry: self.retry,
                timeout_ms: self.timeout_ms,
                artifacts: self.artifacts.clone(),
            },
            bindings,
        ))
    }
}
