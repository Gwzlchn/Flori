//! SQLite 持久化边界。

#![forbid(unsafe_code)]

mod sqlite;

pub use sqlite::{FinalAiUsage, Lease, StartAiUsage, Store, StoreError, UsageRecord};
