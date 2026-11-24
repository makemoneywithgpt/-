package main

import "time"

type Behavior string

const (
	Idle  Behavior = "idle_basic"
	Can   Behavior = "drink_can"
	Lemon Behavior = "lemon_drink"
	Soup  Behavior = "hangover_soup"
	Work  Behavior = "work_pull"
)

var actions = []struct {
	kind Behavior
	name string
}{
	{Idle, "기본 대기"}, {Can, "캔 음료"}, {Lemon, "레몬 음료"},
	{Soup, "해장국"}, {Work, "풀뽑기"},
}

type choice struct {
	kind   Behavior
	weight int
}

func choicesAt(now time.Time, s Settings, click bool) []choice {
	if click {
		return []choice{{Can, 6}, {Lemon, 4}, {Soup, 1}}
	}
	status := statusAt(now, s).Label
	switch status {
	case "done":
		return []choice{{Lemon, 5}}
	case "lunch":
		return []choice{{Soup, 7}}
	case "before", "weekend":
		return nil
	}
	if now.Sub(timeAt(now, s.Start)) < 15*time.Minute {
		return nil
	}
	if timeAt(now, s.End).Sub(now) <= time.Hour {
		return []choice{{Can, 5}, {Lemon, 4}}
	}
	if sinceLunch := now.Sub(timeAt(now, s.LunchEnd)); sinceLunch >= 0 {
		if sinceLunch < 30*time.Minute {
			return []choice{{Soup, 5}, {Can, 4}, {Work, 3}}
		}
		return []choice{{Can, 5}, {Work, 4}}
	}
	if now.Weekday() == time.Monday {
		return []choice{{Work, 16}, {Soup, 1}}
	}
	return []choice{{Work, 5}}
}

func selectAction(choices []choice, clips map[Behavior]clip, last Behavior, roll int) Behavior {
	total := 0
	for _, item := range choices {
		if _, ok := clips[item.kind]; ok && item.kind != last {
			total += item.weight
		}
	}
	if total == 0 {
		return Idle
	}
	roll %= total
	for _, item := range choices {
		if _, ok := clips[item.kind]; !ok || item.kind == last {
			continue
		}
		if roll < item.weight {
			return item.kind
		}
		roll -= item.weight
	}
	return Idle
}

func restFor(frequency, roll int) time.Duration {
	switch frequency {
	case 1:
		return time.Duration(20+roll%11) * time.Second
	case 2:
		return time.Duration(45+roll%16) * time.Second
	default:
		return time.Duration(20+roll%41) * time.Second
	}
}

// Manual requests replace the pending slot, while the current clip plays out.
type playback struct {
	action, last, pending Behavior
	started, until, ready time.Time
}

func (p *playback) start(kind Behavior, now time.Time, duration time.Duration) {
	p.action, p.started, p.until = kind, now, now.Add(duration)
	if kind != Idle {
		p.last = kind
	}
}

func (p *playback) request(kind Behavior) {
	p.pending = kind
}

func (p *playback) advance(now time.Time) {
	if p.action != Idle && !now.Before(p.until) {
		p.start(Idle, now, 0)
		p.ready = now.Add(300 * time.Millisecond)
	}
}

func (p *playback) take(now time.Time) Behavior {
	if p.action != Idle || now.Before(p.ready) {
		return ""
	}
	kind := p.pending
	p.pending = ""
	return kind
}

// With one eligible clip, reserve an additional idle interval before repeating it.
func (p *playback) automatic(choices []choice, clips map[Behavior]clip, roll int) Behavior {
	kind := selectAction(choices, clips, p.last, roll)
	if kind == Idle && selectAction(choices, clips, Idle, roll) != Idle {
		p.last = Idle
	}
	return kind
}
