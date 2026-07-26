+++
date = '2026-07-26T22:11:56+08:00'
draft = true
title = '訊息對話排程演算法'
+++

我經常不清楚如何決定回應訊息的時間。有時會擔心太快會使人感到厭煩，太慢或許又會遭到漠視。儘管最後得知回應速度並非唯一，也不是最主要的評判標準，我還是認爲針對回應訊息做排程可以幫助我節省一些精力。

只是我也很清楚，這並不是一件常見的舉動。而不常見的舉動，或許也常遭到鄙視。覺得這樣做不符合你的想法的，就當作看了個笑話吧。

## 分類

對不同的新進訊息 (`new_message`)，我們可以將其分為以下幾類：

- 即時 (`real_time`)
- 按比例等待 (`proportional_wait`)
- 延遲 (`delayed`)
- 拒絕 (`rejected`)

這幾類主要是因人而異，也會因為對方新的行為模式，以及我的想法而有所改動。我們暫時使用這個結構 (Message) 表達一則訊息。

```rust
enum MessageClass {
    RealTime,
    ProportionalWait(f32, f32),
    Delayed(Duration),
    Rejected,
}

struct Message {
    message_class: MessageClass,
    incoming_time: Time,
}
```

其中，ProportionalWait 表示按比例等待，不過這時候你或許會好奇，為什麼他有兩個數字。答案是他是下限與上限，作為範例，假設上次等待時間是 6 hr，而 ProportionalWait 的兩個數字分別為 0.8 和 1.2 ，這就表示回應的時間大致會在 4.8 hr 到 7.2 hr 之間。而 Delayed 表示延遲，這裡我們使用 Duration 來發出這則訊息的人至少會需要等多久。

這裡就必須要在引入一個結構用以表達一個訊息發送的實體 (MsgEntity)。為了方便人類簡單的做運算，這設計可謂是相當簡單。

```rust
enum MsgEntityClass {
    RealTime,
    ProportionalWait(f32, f32),
    Delayed(Duration),
    Rejected,
}

struct MsgEntity {
    msg_entity_class: MsgEntityClass,
    first_pending_msg: Message,
    last_waiting_time: Duration,
}
```


## 政策

這裡的政策描述的是一個實體從一個類別轉移到另一個類別的過程，以及更改這些常數的關鍵。


## 實作
