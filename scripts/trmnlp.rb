#!/usr/bin/env ruby
# frozen_string_literal: true

require 'trmnlp/cli'

# trmnlp 0.11.0's vendored schema predates the hosted location picker:
# https://help.trmnl.com/en/articles/10513740-custom-plugin-form-builder
# Extend only this documented type. All other upstream validation stays active.
# Once upstream knows lat_lon this shim has no effect and can be removed.
if defined?(TRMNLP::FormField) && !TRMNLP::FormField.field_types.include?('lat_lon')
  module LocationPickerField
    def field_types
      super | ['lat_lon']
    end
  end
  TRMNLP::FormField.singleton_class.prepend(LocationPickerField)
end

if $PROGRAM_NAME == __FILE__
  ENV['TZ'] = 'UTC'
  begin
    TRMNLP::CLI.start
  rescue TRMNLP::Error => e
    warn "Error: #{e.message}"
    exit 1
  rescue Interrupt
    exit 1
  end
end
