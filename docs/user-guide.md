# Using the boiler page

For everyone at home. Open `http://<address of the Orchestrator Pi>:8080/` on a phone or PC
connected to the home network. Nothing to install.

## The short version

* The plan for the hot water lives in **Google Calendar**. The page shows it and the live state.
* Want hot water **now**? Press **On**. Want to cancel the plan for a while? Press **Off**.
  Press **Auto** to go back to the plan.
* A hand-given order lasts about **3 hours**, then the boiler returns to Auto by itself.

## The top bar

Three small lights tell if everything is connected:

| Light | Green means | If it is not green |
|---|---|---|
| **Boiler** | The boiler Pi answers. | The buttons are disabled and a banner says "Boiler not responding". The boiler keeps following its last plan. |
| **Calendar** | The calendar was read in the last few minutes. | The last plan stays in force. |
| **files** | The plan was delivered to the boiler. | It keeps trying; you do not need to do anything. |

## Status tab

* **Mode** shows what the boiler is doing now:
  * **Auto** follows the calendar plan,
  * **Manual** is an order somebody gave by hand (the line under it says when it ends),
  * **Off** keeps the heating off.
* **On / Off / Auto** buttons: **On** heats for a little more than the hot water you have now (never
  more than 5 showers), **Off** stops the heating, **Auto** returns to the plan.
* **Heater** says if the heating element is working right now, and when the next heating starts.
* **Showers available** is how much hot water is ready, counted in showers. The line marked
  *Target* is what the heater heats up to, *Min* is the level it keeps at all times. Above 5 is not
  allowed. Two values are shown (new and old way of measuring); the **new** one is the better one.
* **Boiler temperature** shows the water at the top, middle and bottom of the tank. The colour says
  what the water is good for:

  | Colour | Temperature | Meaning |
  |---|---|---|
  | Blue | below 30° | cold |
  | Teal | 30-40° | lukewarm |
  | Green | 40-50° | hot, good for a shower |
  | Amber | 50-60° | very hot (you mix it with cold water) |
  | Red | 60-70° | scalding |
  | Dark red | above 70° | extremely hot |

* **Last 24 hours** is the chart of the water level, with the times the heater was on.

## Boiler schedule and Timer schedule tabs

These show the plan that was read from Google Calendar.

* **Boiler schedule:** pick a day, see the slots. Each slot starts at a time and lasts until the next
  slot. It has a **Target** and a **Min** (both in showers, at most 5).
* **Timer schedule:** switches for the other relays (for example garden lights): which relay, which
  days, when it goes on and off. Times like "sunset" are turned into clock times for each day.

The **Add**, **Edit** and **Delete** buttons only appear when editing is switched on. If it is off,
change the plan in Google Calendar instead; the page picks it up within a few minutes. A boiler event
looks like `Boiler: target=3.5; min=2`; a timer event like
`Timer: relaynum 4 start sunset +30 end 23:00`.

## Relays tab

Switch one relay on for a number of seconds (more than 10), or off. The first two relays belong to the
boiler itself and are locked; they cannot be switched here.

## System tab

* **Connections** lists the boiler, the calendar and the last delivery of the plan files.
* **Read calendar now** reads the calendar immediately instead of waiting.
* **Push files now** sends the plan to the boiler again.
* **Recent events** is a short log of what happened.

## Log in

If a **Log in** button is shown, a password is needed for actions (On / Off / Auto, edits, relays).
Looking at the page needs no password.

## When something looks wrong

| You see | What it means and what to do |
|---|---|
| "Boiler not responding" | The boiler Pi or the network is down. Nothing to press; the boiler follows its last plan. Check the power and the Wi-Fi. |
| "Google authorization needed" | The link to your Google account expired. Ask whoever looks after the system to renew it. The last plan stays in force. |
| On says "status is unknown / too old" | There is no fresh reading from the boiler yet. Wait a minute and try again. |
| My calendar change does not show | Wait up to 5 minutes, or press **Read calendar now** on the System tab. Check that the event title is written like the examples above. |
| An event is missing from the plan | Open the System tab: events that could not be understood are listed there with the reason. |
