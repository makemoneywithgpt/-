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

func TestSingleActionHasAdditionalIdleInterval(t *testing.T) {
	p := playback{last: Work}
	options := []choice{{Work, 1}}
	clips := map[Behavior]clip{Work: {}}
	if p.automatic(options, clips, 0) != Idle || p.automatic(options, clips, 0) != Work {
		t.Fatal("only available action should repeat after one skipped interval")
	}
}
