#!/usr/bin/env ruby
# frozen_string_literal: true

require 'yaml'
require 'trmnl/liquid'
require_relative 'trmnlp'

settings = YAML.safe_load_file(File.expand_path('../src/settings.yml', __dir__))
fields = settings.fetch('custom_fields')
raise 'Invalid form schema' unless TRMNLP::FormField.validate_all(fields).empty?
location = fields.find { |field| field['keyname'] == 'lat_lon' }
raise 'Missing location picker' unless location && location['field_type'] == 'lat_lon'
raise 'Legacy coordinate inputs still shown' if fields.any? { |f| %w[latitude longitude].include?(f['keyname']) }
age = fields.find { |field| field['keyname'] == 'max_age_minutes' }
raise 'Position age must stay visible' if age.nil? || age.key?('group')
invalid = { 'keyname' => 'test', 'name' => 'Test', 'field_type' => 'not_a_real_type' }
raise 'Unknown field validation was bypassed' if TRMNLP::FormField.validate(invalid).empty?

template = Liquid::Template.parse(settings.fetch('polling_url'), environment: TRMNL::Liquid.new)
cases = [
  [{ 'lat_lon' => ' 47.6062, -122.3321 ' }, '47.6062/-122.3321'],
  [{ 'lat_lon' => '0,0' }, '0/0'],
  [{ 'latitude' => '37.7749', 'longitude' => '-122.4194' }, '37.7749/-122.4194'],
  [{ 'lat_lon' => '47.6062,-122.3321', 'latitude' => '1', 'longitude' => '2' }, '47.6062/-122.3321']
]
cases.each do |values, coordinates|
  actual = template.render!(values.merge('radius_nm' => '20'))
  expected = "https://api.adsb.lol/v2/point/#{coordinates}/20"
  raise "Wrong polling URL: #{actual.inspect}" unless actual == expected
end
puts 'Form schema and polling URL checks passed.'
