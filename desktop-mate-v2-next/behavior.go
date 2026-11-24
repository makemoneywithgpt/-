package main

import "time"

type Behavior string

const (
	Idle  Behavior = "idle"
	Drink Behavior = "drink"
	Doze  Behavior = "doze"
)

var actions = []struct {
	kind Behavior
	name string
}{
	{Idle, "기본 대기"},
	{Drink, "음료 마시기"},
	{Doze, "졸기"},
}

func restFor(frequency, roll int) time.Duration {
	switch frequency {
	case 1:
		return time.Duration(7+roll%9) * time.Second
	case 2:
		return time.Duration(27+roll%25) * time.Second
	default:
		return time.Duration(13+roll%18) * time.Second
	}
}
