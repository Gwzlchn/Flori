use std::fmt;

use serde::de::{self, MapAccess, SeqAccess, Visitor};
use serde::{Deserialize, Deserializer};

use super::*;

pub(super) enum RawInput {
    Bool(bool),
    Number(String),
    String(String),
    List(Vec<Self>),
}

pub(super) fn input_reference<'a>(
    executor: Executor,
    field: &str,
    value: &'a InputValue,
) -> Result<&'a Reference, CompileError> {
    let InputValue::Reference(reference) = value else {
        return Err(invalid());
    };
    matches!(
        (executor, field, reference),
        (
            Executor::DocumentAcquire | Executor::VideoAcquire | Executor::VideoSubscription,
            "source",
            Reference::Source
        ) | (
            Executor::CoreValidate,
            "source",
            Reference::NeedArtifact { .. }
        ) | (
            _,
            "pdf"
                | "video"
                | "document"
                | "subtitle"
                | "transcript"
                | "frames"
                | "mechanical_note"
                | "validated",
            Reference::NeedArtifact { .. }
        ) | (_, "notes", Reference::Need(_))
            | (_, "prompt", Reference::Prompt(_))
            | (_, "profile", Reference::DomainProfile)
    )
    .then_some(reference)
    .ok_or_else(invalid)
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct RawTask {
    pub(super) executor: Executor,
    #[serde(default, rename = "with", deserialize_with = "deserialize_inputs")]
    pub(super) inputs: BTreeMap<String, RawInput>,
    #[serde(default)]
    pub(super) needs: Vec<String>,
    #[serde(default)]
    pub(super) rules: Vec<RawRule>,
    #[serde(default)]
    pub(super) tags: Vec<String>,
    #[serde(default)]
    pub(super) retry: u8,
    pub(super) timeout: String,
    #[serde(default)]
    pub(super) artifacts: Vec<ArtifactDeclaration>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct RawRule {
    #[serde(rename = "if")]
    pub(super) expression: String,
}

struct RawPipeline(BTreeMap<String, RawTask>);

pub(super) fn parse(yaml: &[u8]) -> Result<BTreeMap<String, RawTask>, CompileError> {
    let text = std::str::from_utf8(yaml).map_err(|_| invalid())?;
    if contains_yaml_reference_syntax(text) {
        return Err(invalid());
    }
    let RawPipeline(tasks) = serde_yaml_ng::from_str(text).map_err(|_| invalid())?;
    Ok(tasks)
}

impl<'de> Deserialize<'de> for RawPipeline {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        struct PipelineVisitor;

        impl<'de> Visitor<'de> for PipelineVisitor {
            type Value = RawPipeline;

            fn expecting(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
                formatter.write_str("a task map")
            }

            fn visit_map<A: MapAccess<'de>>(self, mut map: A) -> Result<Self::Value, A::Error> {
                let mut tasks = BTreeMap::new();
                while let Some(key) = map.next_key::<String>()? {
                    if tasks.contains_key(&key) {
                        return Err(de::Error::custom("duplicate task key"));
                    }
                    tasks.insert(key, map.next_value()?);
                }
                Ok(RawPipeline(tasks))
            }
        }

        deserializer.deserialize_map(PipelineVisitor)
    }
}

impl<'de> Deserialize<'de> for RawInput {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        struct InputVisitor;

        impl<'de> Visitor<'de> for InputVisitor {
            type Value = RawInput;

            fn expecting(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
                formatter.write_str("a bool, number, string, or list")
            }

            fn visit_bool<E: de::Error>(self, value: bool) -> Result<Self::Value, E> {
                Ok(RawInput::Bool(value))
            }

            fn visit_i64<E: de::Error>(self, value: i64) -> Result<Self::Value, E> {
                Ok(RawInput::Number(value.to_string()))
            }

            fn visit_u64<E: de::Error>(self, value: u64) -> Result<Self::Value, E> {
                Ok(RawInput::Number(value.to_string()))
            }

            fn visit_f64<E: de::Error>(self, value: f64) -> Result<Self::Value, E> {
                if value.is_finite() {
                    Ok(RawInput::Number(value.to_string()))
                } else {
                    Err(E::custom("non-finite input number"))
                }
            }

            fn visit_str<E: de::Error>(self, value: &str) -> Result<Self::Value, E> {
                Ok(RawInput::String(value.to_owned()))
            }

            fn visit_string<E: de::Error>(self, value: String) -> Result<Self::Value, E> {
                Ok(RawInput::String(value))
            }

            fn visit_seq<A: SeqAccess<'de>>(self, mut seq: A) -> Result<Self::Value, A::Error> {
                let mut values = Vec::new();
                while let Some(value) = seq.next_element()? {
                    values.push(value);
                }
                Ok(RawInput::List(values))
            }
        }

        deserializer.deserialize_any(InputVisitor)
    }
}

fn deserialize_inputs<'de, D: Deserializer<'de>>(
    deserializer: D,
) -> Result<BTreeMap<String, RawInput>, D::Error> {
    struct InputsVisitor;

    impl<'de> Visitor<'de> for InputsVisitor {
        type Value = BTreeMap<String, RawInput>;

        fn expecting(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
            formatter.write_str("an executor input map")
        }

        fn visit_map<A: MapAccess<'de>>(self, mut map: A) -> Result<Self::Value, A::Error> {
            let mut inputs = BTreeMap::new();
            while let Some(key) = map.next_key::<String>()? {
                if inputs.contains_key(&key) {
                    return Err(de::Error::custom("duplicate executor input"));
                }
                inputs.insert(key, map.next_value()?);
            }
            Ok(inputs)
        }
    }

    deserializer.deserialize_map(InputsVisitor)
}

fn contains_yaml_reference_syntax(input: &str) -> bool {
    input.lines().any(|line| {
        let (mut single, mut double, mut escape) = (false, false, false);
        let chars = line.char_indices().collect::<Vec<_>>();
        for (index, (offset, character)) in chars.iter().enumerate() {
            if *character == '#' && !single && !double {
                break;
            }
            if *character == '\'' && !double {
                single = !single;
                continue;
            }
            if *character == '"' && !single && !escape {
                double = !double;
                continue;
            }
            if !single && !double && matches!(character, '&' | '*') {
                let previous = line[..*offset].chars().next_back();
                let next = chars.get(index + 1).map(|(_, character)| *character);
                if previous.is_none_or(|c| c.is_whitespace() || "[{,:?-".contains(c))
                    && next.is_some_and(|c| !c.is_whitespace())
                {
                    return true;
                }
            }
            escape = double && *character == '\\' && !escape;
            if *character != '\\' {
                escape = false;
            }
        }
        false
    })
}
