# Collection of Home Assistant python scripts

This is very much about experimenting with pyscript and calculating savings from solar

## Requirements

- Home Assistant
- Hacs
- Pyscript
- Nordpool or similar electricity price sensor
- Purchased and sold electricity total energy sensors
- Solar panel total energy sensor

## Usage

- Create the necessary `input_number` sensor in Home Assistant
- Update the read sensors with what your sensors are named as
- Copy the files to you Home Assistant `config/pyscript/` folder
- You should now see your `input_number` sensors update at 2 minutes past each hour

## Note

The scripts write to input number helpers that have to be created in Home Assistant before taking the scripts into use. I've not figured out a way to make them persist properly if created through pyscript. You will also need to create a template sensor for each input number if you want to use them in utility meters as utility meters can't use input numbers as input sensor.

UpdateSpotPriceSensors requires NordPool sensor.

## Scripts

List of scripts and what they do

### SolarSavings

- Calculates overall savings from having solar panels compared to not having solar panels.
- Calculates car charging costs and savings from having solar panels
- Calculates heat pump costs and savings from having solar panels

### UpdateSpotPriceSensors

Creates an easy to use number that will range from 0 to 3 based on long term and short term electricity cost.

## Todo

- Find a way to persist a sensor created in pyscript properly so input_number sensors are not needed
- Make read sensors configurable somehow so you don't have to edit the code if someone else wants to use the scripts
- Add more fun scripts :)
