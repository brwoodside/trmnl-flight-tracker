#!/usr/bin/env ruby
# frozen_string_literal: true

require 'json'
require 'fileutils'
require 'trmnl/liquid'

root = File.expand_path('..', __dir__)
fixtures = JSON.parse(File.read("#{root}/_build/view-fixtures.json"))
output = "#{root}/_build/views"
FileUtils.mkdir_p(output)
render_png = ARGV.delete('--png')
raise "Unknown arguments: #{ARGV.join(' ')}" unless ARGV.empty?

if render_png
  require 'trmnlp/browser_pool'
  require 'trmnlp/firefox_driver'
  require 'trmnlp/screen_generator'
  require 'trmnlp/screenshot'
  pool = TRMNLP::BrowserPool.new(driver_factory: TRMNLP::FirefoxDriver.method(:build))
  screenshot = TRMNLP::Screenshot.new(pool: pool)
end

devices = [
  ['og-landscape', 800, 480, 'screen--og screen--md screen--density-1x screen--1bit', 1],
  ['og-portrait', 480, 800, 'screen--og screen--md screen--density-1x screen--1bit screen--portrait', 1],
  ['x-landscape', 1872, 1404, 'screen--v2 screen--lg screen--density-2x screen--4bit', 4],
  ['x-portrait', 1404, 1872, 'screen--v2 screen--lg screen--density-2x screen--4bit screen--portrait', 4]
]
views = {
  'full' => nil, 'half_horizontal' => 'mashup--1Tx1B',
  'half_vertical' => 'mashup--1Lx1R', 'quadrant' => 'mashup--2x2'
}

begin
  fixtures.each do |scenario, data|
    views.each do |view, mashup|
      source = File.read("#{root}/src/shared.liquid") + File.read("#{root}/src/#{view}.liquid")
      body = Liquid::Template.parse(source, environment: TRMNL::Liquid.new).render!(data)
      text = body.gsub(/<[^>]*>/, " ")
      raise "Missing layout/title bar: #{view}/#{scenario}" unless body.scan(/<div class="layout(?:\s|")/).length == 1 && body.scan(/<div class="title_bar(?:\s|")/).length == 1
      if view == 'quadrant'
        expected = {
          'empty' => 'No aircraft within 20.0 nm',
          'provider_error' => 'Aircraft data unavailable',
          'configuration_error' => 'Tracker not configured'
        }[scenario]
        raise "Wrong quadrant message: #{scenario}" if expected && !text.include?(expected)
        if scenario.end_with?('error') && text.include?('Clear sky')
          raise "Error shown as clear sky: #{scenario}"
        end
      end
      body = "<div class='view view--#{view}'>#{body}</div>"
      body = "<div class='mashup #{mashup}'>#{body}</div>" if mashup
      devices.each do |device, width, height, classes, depth|
        html = <<~HTML
          <!DOCTYPE html><html><head><meta charset="utf-8">
          <link rel="stylesheet" href="https://trmnl.com/css/3.3.2/plugins.css">
          <script src="https://trmnl.com/js/3.3.2/plugins.js"></script>
          </head><body class="environment trmnl"><div class="screen #{classes}">#{body}</div></body></html>
        HTML
        basename = "#{output}/#{scenario}-#{view}-#{device}"
        File.write("#{basename}.html", html)
        # Full device matrix for normal content and radar rotation; error-state
        # screenshots exercise the changed quadrant. All states render to HTML.
        render_screenshot = %w[aircraft partial_route].include?(scenario) ||
                            (scenario == 'rotated' && view == 'full') ||
                            (!%w[rotated partial_route].include?(scenario) && view == 'quadrant')
        next unless render_png && render_screenshot

        image = TRMNLP::ScreenGenerator.new(html, screenshot: screenshot, width: width,
                                           height: height, color_depth: depth).process
        FileUtils.cp(image.path, "#{basename}.png")
        image.close!
        puts "Rendered #{scenario}/#{view}/#{device}"
      end
    end
  end
ensure
  pool&.shutdown
end
puts "View checks passed: #{fixtures.size} states × #{views.size} views × #{devices.size} devices."
