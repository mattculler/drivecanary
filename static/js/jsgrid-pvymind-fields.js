var DiskTypeField = function(config) {
  jsGrid.Field.call(this, config);
};
 
DiskTypeField.prototype = new jsGrid.Field({
 
  css: "disk-type-field",            // redefine general property 'css'
  align: "center",              // redefine general property 'align'

  sorter: function(disk1, disk2) {
    // Sort SSDs above HDDs
    return disk2 - disk1;
  },

  itemTemplate: function(is_hard_disk) {
    var src = "/static/images/icons/";
    var alt;
    if (is_hard_disk === true) {
      src += "icons8-hdd.png";
      alt = "spinning rust";
    }
    else if (is_hard_disk === false) {
      src += "icons8-ssd.png";
      alt = "solid state drive";
    }
    else {
      src += "icons8-question-mark.png";
      alt = "Unknown type";
    }
    return $("<img>", {
      "src": src,
      "title": alt,
      "alt": alt
    });
  },

  insertTemplate: function(value) { return; },
  editTemplate: function(value) { return; },
  insertValue: function() { return },
  editValue: function() { return; },
});
 
jsGrid.fields.disktype = DiskTypeField;



var DateTimeField = function(config) {
    jsGrid.Field.call(this, config);
};
 
DateTimeField.prototype = new jsGrid.Field({
 
  css: "date-field",            // redefine general property 'css'
  align: "center",              // redefine general property 'align'

  sorter: function(date1, date2) {
    return new Date(date1) - new Date(date2);
  },

  itemTemplate: function(value) {
    return new Date(value).toLocaleString();
  },

/*  insertTemplate: function(value) {
    return this._insertPicker = $("<input>").datepicker({ defaultDate: new Date() });
  },

  editTemplate: function(value) {
    return this._editPicker = $("<input>").datepicker().datepicker("setDate", new Date(value));
  },

  insertValue: function() {
    return this._insertPicker.datepicker("getDate").toISOString();
  },

  editValue: function() {
    return this._editPicker.datepicker("getDate").toISOString();
  }*/
});
 
jsGrid.fields.date = DateTimeField;




var CapacityField = function(config) {
  jsGrid.Field.call(this, config);
};
 
CapacityField.prototype = new jsGrid.Field({
 
  css: "capacity-field",            // redefine general property 'css'
  align: "center",              // redefine general property 'align'

  sorter: function(disk1, disk2) {
    return disk1 - disk2;
  },

  itemTemplate: function(size_bytes) {
    return $("<span>", {
      "html": filesize(size_bytes),
      "alt": size_bytes,
      "title": size_bytes
    });
  },

  insertTemplate: function(value) { return; },
  editTemplate: function(value) { return; },
  insertValue: function() { return },
  editValue: function() { return; },
});
 
jsGrid.fields.capacity = CapacityField;


