package main

import (
	"testing"
	"time"
)

func TestPolicies(t *testing.T) {
	s := defaultSettings()
	clips := map[Behavior]clip{Work: {}, Soup: {}, Can: {}}
	for _, test := range []struct {
		hour, minute, roll int
		last, want         Behavior
	}{
		{9, 5, 0, "", Idle}, {10, 0, 0, "", Work}, {10, 0, 16, "", Soup},
		{12, 15, 0, "", Soup}, {16, 0, 0, Can, Work}, {17, 10, 0, "", Can},
		{18, 0, 0, "", Idle},
	} {
		got := selectAction(choicesAt(tdate(23, test.hour, test.minute, 0), s, false), clips, test.last, test.roll)
		if got != test.want {
			t.Errorf("%d:%d: got %q, want %q", test.hour, test.minute, got, test.want)
		}
	}
	for mode := 0; mode < 3; mode++ {
		for roll := 0; roll < 100; roll++ {
			if d := restFor(mode, roll); d < 20*time.Second || d > 60*time.Second {
				t.Fatal(d)
			}
		}
	}
}

func TestQueuedPreviewDoesNotInterrupt(t *testing.T) {
	now := time.Now()
	var p playback
	p.start(Can, now, time.Second)
	p.request(Soup)
	if p.take(now.Add(time.Millisecond)) != "" || p.action != Can {
		t.Fatal("interrupted clip")
	}
	p.advance(now.Add(time.Second))
	if p.action != Idle || p.take(now.Add(time.Second)) != "" {
		t.Fatal("idle transition skipped")
	}
	if got := p.take(now.Add(1300 * time.Millisecond)); got != Soup || p.last != Can {
		t.Fatal(got, p.last)
	}
	if got := p.take(now.Add(1400 * time.Millisecond)); got != "" {
		t.Fatal(got)
	}
}

func TestAutomaticTransitionsAndDiagnostics(t *testing.T) {
	now := tdate(23, 10, 0, 0)
	p := playback{action: Idle, last: Work}
	clips := map[Behavior]clip{Work: {}}
	s := defaultSettings()
	status := func(at, due time.Time, preview bool) autoStatus {
		return automaticStatus(at, due, p, s, clips, preview, 0)
	}
	if status(now, now.Add(20*time.Second), false).action != Idle ||
		status(now.Add(20*time.Second), now.Add(20*time.Second), false).action != Work {
		t.Fatal("scheduled work should start at the deadline without a click")
	}
	lunch := tdate(23, 12, 20, 0)
	if result := status(lunch, lunch, false); result.action != Idle || result.reason == "" {
		t.Fatal("expected unavailable time-slot diagnostics")
	}
	s.QuietMode = true
	if status(lunch, lunch, false).action != Idle {
		t.Fatal("quiet mode should disable automatic playback")
	}
	due := lunch.Add(previewInterval)
	if status(due.Add(-time.Millisecond), due, true).action != Idle || status(due, due, true).action != Work {
		t.Fatal("five-second test should play at its deadline even at lunch and in quiet mode")
	}
	p.start(Work, due, 1720*time.Millisecond)
	p.advance(due.Add(1719 * time.Millisecond))
	if p.action != Work || status(due.Add(time.Second), due, true).action != Idle {
		t.Fatal("clip was interrupted or restarted early")
	}
	p.advance(due.Add(1720 * time.Millisecond))
	if p.action != Idle {
		t.Fatal("special must return to idle, even without an idle image")
	}
}
