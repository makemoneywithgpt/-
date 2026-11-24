package main

import (
	"fmt"
	"time"
)

type Settings struct {
	Start       string `json:"start"`
	End         string `json:"end"`
	LunchStart  string `json:"lunch_start"`
	LunchEnd    string `json:"lunch_end"`
	Scale       int    `json:"scale"`
	AlwaysOnTop bool   `json:"always_on_top"`
	QuietMode   bool   `json:"quiet_mode"`
	Frequency   int    `json:"frequency"`
	X           int    `json:"x"`
	Y           int    `json:"y"`
	Placed      bool   `json:"placed"`
}

func defaultSettings() Settings {
	return Settings{Start: "09:00", End: "18:00", LunchStart: "12:00", LunchEnd: "13:00", Scale: 100, AlwaysOnTop: true}
}

func minutesOfDay(v string) (int, bool) {
	var h, m int
	if _, err := fmt.Sscanf(v, "%d:%d", &h, &m); err != nil || len(v) != 5 || h < 0 || h > 23 || m < 0 || m > 59 {
		return 0, false
	}
	return h*60 + m, true
}
func validSettings(s Settings) Settings {
	d := defaultSettings()
	if _, ok := minutesOfDay(s.Start); !ok {
		s.Start = d.Start
	}
	if _, ok := minutesOfDay(s.End); !ok {
		s.End = d.End
	}
	if _, ok := minutesOfDay(s.LunchStart); !ok {
		s.LunchStart = d.LunchStart
	}
	if _, ok := minutesOfDay(s.LunchEnd); !ok {
		s.LunchEnd = d.LunchEnd
	}
	if s.Scale != 75 && s.Scale != 90 && s.Scale != 100 && s.Scale != 120 && s.Scale != 140 {
		s.Scale = d.Scale
	}
	if s.Frequency < 0 || s.Frequency > 2 {
		s.Frequency = 0
	}
	return s
}
func timeAt(now time.Time, hm string) time.Time {
	m, _ := minutesOfDay(hm)
	return time.Date(now.Year(), now.Month(), now.Day(), m/60, m%60, 0, 0, now.Location())
}

type Status struct {
	Label     string
	Remaining time.Duration
	Timed     bool
}

func statusAt(now time.Time, s Settings) Status {
	if now.Weekday() == time.Saturday || now.Weekday() == time.Sunday {
		return Status{Label: "weekend"}
	}
	start, end := timeAt(now, s.Start), timeAt(now, s.End)
	lunchStart, lunchEnd := timeAt(now, s.LunchStart), timeAt(now, s.LunchEnd)
	if now.Before(start) {
		return Status{"before", start.Sub(now), true}
	}
	if !end.After(start) || !now.Before(end) {
		return Status{Label: "done"}
	}
	if !lunchEnd.Before(lunchStart) && !now.Before(lunchStart) && now.Before(lunchEnd) {
		return Status{"lunch", lunchEnd.Sub(now), true}
	}
	if end.Sub(now) <= 40*time.Minute {
		return Status{"soon", end.Sub(now), true}
	}
	if now.Before(lunchStart) && lunchStart.Sub(now) <= 30*time.Minute && lunchEnd.After(lunchStart) {
		return Status{"lunch_soon", lunchStart.Sub(now), true}
	}
	if now.Hour() >= 15 {
		return Status{"afternoon", end.Sub(now), true}
	}
	return Status{"work", end.Sub(now), true}
}
func countdownFormat(d time.Duration) string {
	if d < 0 {
		d = 0
	}
	seconds := int(d.Seconds())
	if seconds > 359999 {
		seconds = 359999
	}
	return fmt.Sprintf("%02d:%02d:%02d", seconds/3600, (seconds%3600)/60, seconds%60)
}
