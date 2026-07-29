---
date: '2026-07-26T22:11:56+08:00'
title: '訊息對話排程演算法'
description: "A migration to Hugo"
draft: false
tags: ["misc"]
---

我經常不清楚如何決定回應訊息的時間。有時會擔心太快會使人感到厭煩，太慢或許又會遭到漠視。儘管最後得知回應速度並非唯一，也不是最主要的評判標準，我還是認爲針對回應訊息做排程可以幫助我節省一些精力。雖然聽起來是挺抽象的。


## 分類

對不同的新進訊息 (`new_message`)，我們可以將其分為以下幾類：

- 即時 (`real_time`)
- 按比例等待 (`proportional_wait`)
- 延遲 (`delayed`)
- 拒絕 (`rejected`)

這幾類主要是因人而異，也會因為對方新的行為模式，以及我的想法而有所改動。我們暫時使用這個結構 (Message) 表達一則訊息。

```rust
#[derive(Debug, Clone, Copy, PartialEq)]
pub enum MessageClass {
    RealTime,
    ProportionalWait(f32, f32),
    Delayed(Duration),
    Rejected,
}
 
#[derive(Debug, Clone, Copy)]
pub struct Message {
    pub message_class: MessageClass,
    pub incoming_time: Time,
}
```

其中，ProportionalWait 表示按比例等待，不過這時候你或許會好奇，為什麼他有兩個數字。答案是他是下限與上限，作為範例，假設上次等待時間是 6 hr，而 ProportionalWait 的兩個數字分別為 0.8 和 1.2 ，這就表示回應的時間大致會在 4.8 hr 到 7.2 hr 之間。而 Delayed 表示延遲，這裡我們使用 Duration 來發出這則訊息的人至少會需要等多久。

這裡就必須要在引入一個結構用以表達一個訊息發送的實體 (MsgEntity)。為了方便人類簡單的做運算，這設計可謂是相當簡單。

```rust
#[derive(Debug, Clone, Copy, PartialEq)]
pub enum MsgEntityClass {
    RealTime,
    ProportionalWait(f32, f32),
    Delayed(Duration),
    Rejected,
}
 
pub struct MsgEntity {
    pub msg_entity_class: MsgEntityClass,
    pub first_pending_msg: Message,
    pub last_waiting_time: Duration,
    pub recent_msg_timestamps: Vec<Time>,
    pub last_rejected_review: Option<Time>,
}
```


## 政策

這裡的政策描述的是一個實體從一個類別轉移到另一個類別的過程，以及更改這些常數的關鍵。首先，所有人預設為 `RealTime` 類別。接著，以下列出幾種可能會被移出 `RealTime` 類別的情況：

1. 訊息等待時間超過 24 小時。
2. 我不想要回應，且訊息的頻率超過 3 次/小時。

如果情況為 1，我將移動他至 `ProportionalWait` 類別。如果情況為 2，則移動他至 `Delayed` 類別。

在一個人為 `ProportionalWait` 的情況下，我會根據等待時間和頻率來計算一個比例等待時間。而移動這個實體到其他類別的情況有以下幾種：

1. 等待時間超過 7 天。
2. 我想要回應的快些。
3. 其訊息具有時效性。
4. 我不再想要回應。

如果情況為 1，我將移動他回 `RealTime` 類別。如果情況為 2，我將改變等待的時間常數，或移動到 `RealTime` 類別。如果情況為 3，我將移動他至 `RealTime` 類別。否則移動至 `Rejected` 類別。

此外，在一個人為 `Delayed` 的情況下，我會等待至少一個時間才在有空時回應。而在這個類別底下的人通常就不會再被移動出去。

`Rejected` 類別底下的人大約每兩個月會重新檢視一次他是否適合移動到其他類別。


## 實作

```rust
use std::time::{Duration, SystemTime};
pub type Time = SystemTime;

#[derive(Debug, Clone, Copy)]
pub enum SubjectiveSignal {
    DontWantToRespond,
    SpeedUp(SpeedUpAction),
    MessageIsUrgent,
    NoLongerWantToRespond,
}
 
#[derive(Debug, Clone, Copy)]
pub enum SpeedUpAction {
    AdjustBounds(f32, f32),
    SwitchToRealTime,
}
 
mod policy {
    use std::time::Duration;
 
    pub const REAL_TIME_TO_PROPORTIONAL: Duration = Duration::from_secs(24 * 3600);
    pub const PROPORTIONAL_TO_REAL_TIME: Duration = Duration::from_secs(7 * 24 * 3600);
    pub const REJECTED_REVIEW_INTERVAL: Duration = Duration::from_secs(60 * 24 * 3600);
    pub const DELAYED_FREQUENCY_THRESHOLD: f32 = 3.0;
    pub const FREQUENCY_WINDOW: Duration = Duration::from_secs(3600);
    pub const DEFAULT_PROPORTIONAL_BOUNDS: (f32, f32) = (0.8, 1.2);
}
 
fn elapsed_since(current: Time, earlier: Time) -> Duration {
    current.duration_since(earlier).unwrap_or(Duration::ZERO)
}
 
fn recent_frequency(msg_entity: &MsgEntity, current_time: Time) -> f32 {
    msg_entity
        .recent_msg_timestamps
        .iter()
        .filter(|&&t| elapsed_since(current_time, t) <= policy::FREQUENCY_WINDOW)
        .count() as f32
}

pub fn update_msg_entity_class(
    msg_entity: &mut MsgEntity,
    current_time: Time,
    signal: Option<SubjectiveSignal>,
) {
    let waiting_time = elapsed_since(current_time, msg_entity.first_pending_msg.incoming_time);
 
    match msg_entity.msg_entity_class {
        MsgEntityClass::RealTime => {
            if waiting_time > policy::REAL_TIME_TO_PROPORTIONAL {
                let (lower, upper) = policy::DEFAULT_PROPORTIONAL_BOUNDS;
                msg_entity.msg_entity_class = MsgEntityClass::ProportionalWait(lower, upper);
            } else if matches!(signal, Some(SubjectiveSignal::DontWantToRespond))
                && recent_frequency(msg_entity, current_time) > policy::DELAYED_FREQUENCY_THRESHOLD
            {
                msg_entity.msg_entity_class = MsgEntityClass::Delayed(msg_entity.last_waiting_time);
            }
        }
 
        MsgEntityClass::ProportionalWait(_, _) => {
            if waiting_time > policy::PROPORTIONAL_TO_REAL_TIME {
                msg_entity.msg_entity_class = MsgEntityClass::RealTime;
                return;
            }
 
            match signal {
                Some(SubjectiveSignal::SpeedUp(SpeedUpAction::AdjustBounds(lower, upper))) => {
                    msg_entity.msg_entity_class = MsgEntityClass::ProportionalWait(lower, upper);
                }
                Some(SubjectiveSignal::SpeedUp(SpeedUpAction::SwitchToRealTime)) => {
                    msg_entity.msg_entity_class = MsgEntityClass::RealTime;
                }
                Some(SubjectiveSignal::MessageIsUrgent) => {
                    msg_entity.msg_entity_class = MsgEntityClass::RealTime;
                }
                Some(SubjectiveSignal::NoLongerWantToRespond) => {
                    msg_entity.msg_entity_class = MsgEntityClass::Rejected;
                    msg_entity.last_rejected_review = Some(current_time);
                }
                _ => {}
            }
        }
 
        MsgEntityClass::Delayed(_) => {
            // 通常不會再被移動出去
        }
 
        MsgEntityClass::Rejected => {
            let due_for_review = match msg_entity.last_rejected_review {
                Some(last) => elapsed_since(current_time, last) > policy::REJECTED_REVIEW_INTERVAL,
                None => true,
            };
 
            if due_for_review {
                msg_entity.last_rejected_review = Some(current_time);
 
                match signal {
                    Some(SubjectiveSignal::MessageIsUrgent) => {
                        msg_entity.msg_entity_class = MsgEntityClass::RealTime;
                    }
                    Some(SubjectiveSignal::SpeedUp(_)) => {
                        let (lower, upper) = policy::DEFAULT_PROPORTIONAL_BOUNDS;
                        msg_entity.msg_entity_class = MsgEntityClass::ProportionalWait(lower, upper);
                    }
                    _ => {}
                }
            }
        }
    }
}

pub fn schedule_msg_entity(msg_entities: &mut Vec<MsgEntity>, is_available: bool) -> Vec<usize> {
    let current_time = Time::now();
    let mut ready_to_respond = Vec::new();
 
    for (index, msg_entity) in msg_entities.iter_mut().enumerate() {
        update_msg_entity_class(msg_entity, current_time, None);
 
        let waiting_time =
            elapsed_since(current_time, msg_entity.first_pending_msg.incoming_time);
 
        let is_due = match msg_entity.msg_entity_class {
            MsgEntityClass::RealTime => true,
 
            MsgEntityClass::ProportionalWait(lower, _upper) => {
                let lower_bound = msg_entity.last_waiting_time.mul_f32(lower);
                waiting_time >= lower_bound
            }
 
            MsgEntityClass::Delayed(min_wait) => is_available && waiting_time >= min_wait,
 
            MsgEntityClass::Rejected => false,
        };
 
        if is_due {
            ready_to_respond.push(index);
        }
    }
 
    ready_to_respond
}
```
